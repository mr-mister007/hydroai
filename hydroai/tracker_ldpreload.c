#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdint.h>
#include <time.h>
#include <errno.h>
#include <sys/uio.h>

#define MAX_IPS 64
#define MAX_PROCS 16
#define MAX_FDS 8192
#define SHM_TABLE_SIZE 256

struct wm_entry {
    pid_t pid;
    volatile uint64_t bytes_sent;
    volatile uint64_t bytes_received;
    volatile uint64_t last_active;
    char comm[32];
};

struct wm_shm {
    volatile uint64_t magic;
    struct wm_entry entries[SHM_TABLE_SIZE];
};

static struct wm_shm *shm = NULL;
static uint32_t ai_ips[MAX_IPS];
static int num_ai_ips = 0;
static char tracked_procs[MAX_PROCS][32];
static int num_tracked_procs = 0;
static int is_tracked_proc = 0;

static struct fd_entry {
    uint8_t tracked;
} fd_info[MAX_FDS] = {};

static int (*real_connect)(int, const struct sockaddr *, socklen_t);
static int (*real_close)(int);
static ssize_t (*real_send)(int, const void *, size_t, int);
static ssize_t (*real_recv)(int, void *, size_t, int);
static ssize_t (*real_sendto)(int, const void *, size_t, int, const struct sockaddr *, socklen_t);
static ssize_t (*real_recvfrom)(int, void *, size_t, int, struct sockaddr *, socklen_t *);
static ssize_t (*real_sendmsg)(int, const struct msghdr *, int);
static ssize_t (*real_recvmsg)(int, struct msghdr *, int);
static ssize_t (*real_write)(int, const void *, size_t);
static ssize_t (*real_writev)(int, const struct iovec *, int);
static ssize_t (*real_read)(int, void *, size_t);

static inline int is_ai_ipv4(uint32_t ip) {
    for (int i = 0; i < num_ai_ips; i++)
        if (ai_ips[i] == ip) return 1;
    return 0;
}

static inline int entry_slot(pid_t pid) {
    struct wm_shm *s = shm;
    if (!s) return -1;
    int slot = (uint32_t)pid % SHM_TABLE_SIZE;
    for (int i = 0; i < SHM_TABLE_SIZE; i++) {
        int idx = (slot + i) % SHM_TABLE_SIZE;
        pid_t existing = __atomic_load_n(&s->entries[idx].pid, __ATOMIC_ACQUIRE);
        if (existing == pid) return idx;
        if (existing == 0) {
            pid_t zero = 0;
            if (__atomic_compare_exchange_n(&s->entries[idx].pid, &zero, pid, 0,
                                            __ATOMIC_RELEASE, __ATOMIC_ACQUIRE)) {
                char path[64];
                snprintf(path, sizeof(path), "/proc/%d/comm", pid);
                FILE *f = fopen(path, "r");
                if (f) {
                    size_t n = fread(s->entries[idx].comm, 1, sizeof(s->entries[idx].comm) - 1, f);
                    if (n > 0 && s->entries[idx].comm[n-1] == '\n')
                        s->entries[idx].comm[n-1] = '\0';
                    fclose(f);
                }
                return idx;
            }
        }
    }
    return -1;
}

static inline void track_bytes(int fd, uint64_t sent, uint64_t received) {
    if (!shm || fd < 0 || fd >= MAX_FDS) return;
    if (!fd_info[fd].tracked) return;
    int slot = entry_slot(getpid());
    if (slot < 0) return;
    if (sent > 0)
        __atomic_fetch_add(&shm->entries[slot].bytes_sent, sent, __ATOMIC_RELAXED);
    if (received > 0)
        __atomic_fetch_add(&shm->entries[slot].bytes_received, received, __ATOMIC_RELAXED);
    __atomic_store_n(&shm->entries[slot].last_active, (uint64_t)time(NULL), __ATOMIC_RELEASE);
}

__attribute__((constructor)) void wm_init(void) {
    real_connect = dlsym(RTLD_NEXT, "connect");
    real_close = dlsym(RTLD_NEXT, "close");
    real_send = dlsym(RTLD_NEXT, "send");
    real_recv = dlsym(RTLD_NEXT, "recv");
    real_sendto = dlsym(RTLD_NEXT, "sendto");
    real_recvfrom = dlsym(RTLD_NEXT, "recvfrom");
    real_sendmsg = dlsym(RTLD_NEXT, "sendmsg");
    real_recvmsg = dlsym(RTLD_NEXT, "recvmsg");
    real_write = dlsym(RTLD_NEXT, "write");
    real_writev = dlsym(RTLD_NEXT, "writev");
    real_read = dlsym(RTLD_NEXT, "read");

    const char *ip_list = getenv("WATERMETER_IPS");
    if (ip_list) {
        char *copy = strdup(ip_list);
        if (copy) {
            char *tok = strtok(copy, ",");
            while (tok && num_ai_ips < MAX_IPS) {
                struct in_addr addr;
                if (inet_pton(AF_INET, tok, &addr) == 1)
                    ai_ips[num_ai_ips++] = addr.s_addr;
                tok = strtok(NULL, ",");
            }
            free(copy);
        }
    }

    const char *proc_list = getenv("WATERMETER_PROC_NAMES");
    if (proc_list) {
        char *copy = strdup(proc_list);
        if (copy) {
            char *tok = strtok(copy, ",");
            while (tok && num_tracked_procs < MAX_PROCS) {
                strncpy(tracked_procs[num_tracked_procs], tok, 31);
                tracked_procs[num_tracked_procs][31] = '\0';
                num_tracked_procs++;
                tok = strtok(NULL, ",");
            }
            free(copy);
        }
    }

    if (num_tracked_procs > 0) {
        char my_comm[32] = {0};
        FILE *fc = fopen("/proc/self/comm", "r");
        if (fc) {
            size_t n = fread(my_comm, 1, sizeof(my_comm) - 1, fc);
            if (n > 0 && my_comm[n-1] == '\n') my_comm[n-1] = '\0';
            fclose(fc);
        }
        for (int i = 0; i < num_tracked_procs; i++) {
            if (strcmp(my_comm, tracked_procs[i]) == 0) {
                is_tracked_proc = 1;
                break;
            }
        }
    }

    const char *shm_path = getenv("WATERMETER_SHM");
    if (shm_path) {
        int fd = open(shm_path, O_RDWR, 0666);
        if (fd >= 0) {
            shm = mmap(NULL, sizeof(struct wm_shm), PROT_READ | PROT_WRITE,
                       MAP_SHARED, fd, 0);
            if (shm == MAP_FAILED) shm = NULL;
            close(fd);
        }
    }
}

int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    int ret = real_connect(sockfd, addr, addrlen);
    if (sockfd >= MAX_FDS || !addr) return ret;
    int matched = 0;
    if (ret == 0 || (ret == -1 && errno == EINPROGRESS)) {
        if (num_ai_ips > 0 && addr->sa_family == AF_INET) {
            struct sockaddr_in *a4 = (struct sockaddr_in *)addr;
            if (is_ai_ipv4(a4->sin_addr.s_addr))
                matched = 1;
        }
        if (!matched && is_tracked_proc) {
            if (addr->sa_family == AF_INET) {
                struct sockaddr_in *a4 = (struct sockaddr_in *)addr;
                uint32_t loopback = htonl(INADDR_LOOPBACK);
                if (a4->sin_addr.s_addr != loopback)
                    matched = 1;
            } else if (addr->sa_family == AF_INET6) {
                struct sockaddr_in6 *a6 = (struct sockaddr_in6 *)addr;
                if (!IN6_IS_ADDR_LOOPBACK(&a6->sin6_addr))
                    matched = 1;
            }
        }
        if (matched)
            fd_info[sockfd].tracked = 1;
    }
    return ret;
}

int close(int fd) {
    if (fd >= 0 && fd < MAX_FDS) fd_info[fd].tracked = 0;
    return real_close(fd);
}

ssize_t send(int sockfd, const void *buf, size_t len, int flags) {
    ssize_t ret = real_send(sockfd, buf, len, flags);
    if (ret > 0) track_bytes(sockfd, ret, 0);
    return ret;
}

ssize_t recv(int sockfd, void *buf, size_t len, int flags) {
    ssize_t ret = real_recv(sockfd, buf, len, flags);
    if (ret > 0) track_bytes(sockfd, 0, ret);
    return ret;
}

ssize_t sendto(int sockfd, const void *buf, size_t len, int flags,
               const struct sockaddr *dest_addr, socklen_t addrlen) {
    if (!fd_info[sockfd].tracked && dest_addr && sockfd < MAX_FDS) {
        int matched = 0;
        if (dest_addr->sa_family == AF_INET) {
            struct sockaddr_in *a4 = (struct sockaddr_in *)dest_addr;
            if (num_ai_ips > 0 && is_ai_ipv4(a4->sin_addr.s_addr))
                matched = 1;
            if (!matched && is_tracked_proc) {
                uint32_t loopback = htonl(INADDR_LOOPBACK);
                if (a4->sin_addr.s_addr != loopback)
                    matched = 1;
            }
        }
        if (matched)
            fd_info[sockfd].tracked = 1;
    }
    ssize_t ret = real_sendto(sockfd, buf, len, flags, dest_addr, addrlen);
    if (ret > 0) track_bytes(sockfd, ret, 0);
    return ret;
}

ssize_t recvfrom(int sockfd, void *buf, size_t len, int flags,
                 struct sockaddr *src_addr, socklen_t *addrlen) {
    ssize_t ret = real_recvfrom(sockfd, buf, len, flags, src_addr, addrlen);
    if (ret > 0) track_bytes(sockfd, 0, ret);
    return ret;
}

ssize_t sendmsg(int sockfd, const struct msghdr *msg, int flags) {
    ssize_t ret = real_sendmsg(sockfd, msg, flags);
    if (ret > 0) track_bytes(sockfd, ret, 0);
    return ret;
}

ssize_t recvmsg(int sockfd, struct msghdr *msg, int flags) {
    ssize_t ret = real_recvmsg(sockfd, msg, flags);
    if (ret > 0) track_bytes(sockfd, 0, ret);
    return ret;
}

ssize_t write(int fd, const void *buf, size_t count) {
    ssize_t ret = real_write(fd, buf, count);
    if (ret > 0) track_bytes(fd, ret, 0);
    return ret;
}

ssize_t writev(int fd, const struct iovec *iov, int iovcnt) {
    ssize_t ret = real_writev(fd, iov, iovcnt);
    if (ret > 0) track_bytes(fd, ret, 0);
    return ret;
}

ssize_t read(int fd, void *buf, size_t count) {
    ssize_t ret = real_read(fd, buf, count);
    if (ret > 0) track_bytes(fd, 0, ret);
    return ret;
}

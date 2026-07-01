import { readFileSync, existsSync } from "node:fs";

const SHM_PATH = "/tmp/watermeter_shm";
const SHM_SIZE = 16392;
const ENTRY_SIZE = 64;
const ENTRIES_OFFSET = 8;

function readSHM() {
  if (!existsSync(SHM_PATH)) return [];
  try {
    const data = readFileSync(SHM_PATH);
    const entries = [];
    for (let i = 0; i < 256; i++) {
      const off = ENTRIES_OFFSET + i * ENTRY_SIZE;
      if (off + ENTRY_SIZE > data.length) break;
      const pid = data.readInt32LE(off);
      if (pid > 0) {
        entries.push({
          pid,
          sent: Number(data.readBigUInt64LE(off + 8)),
          recv: Number(data.readBigUInt64LE(off + 16)),
        });
      }
    }
    return entries;
  } catch {
    return [];
  }
}

function calcWater(sent, recv) {
  const inTok = sent / 5;
  const outTok = recv / 5;
  const ml = (inTok / 1000) * 3 + (outTok / 1000) * 15;
  return ml;
}

let lastTotalSent = 0;
let lastTotalRecv = 0;

export default async function watermeterPlugin() {
  return {
    "experimental.text.complete": async (_input, output) => {
      const entries = readSHM();
      if (!entries.length) return;

      const totalSent = entries.reduce((s, e) => s + e.sent, 0);
      const totalRecv = entries.reduce((s, e) => s + e.recv, 0);
      const deltaSent = totalSent - lastTotalSent;
      const deltaRecv = totalRecv - lastTotalRecv;
      lastTotalSent = totalSent;
      lastTotalRecv = totalRecv;

      if (deltaSent <= 0 && deltaRecv <= 0) return;

      const responseML = calcWater(deltaSent, deltaRecv);
      const totalML = calcWater(totalSent, totalRecv);
      output.text += `\n\n——  🌊 *${responseML.toFixed(1)} mL* for this response · *${totalML.toFixed(1)} mL* total  ——`;
    },
  };
}

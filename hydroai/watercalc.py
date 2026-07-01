WATER_PER_1K_INPUT_TOKENS_ML = 3.0
WATER_PER_1K_OUTPUT_TOKENS_ML = 15.0

ENCRYPTED_BYTES_PER_TOKEN = 5.2
WATER_ML_PER_INPUT_BYTE = WATER_PER_1K_INPUT_TOKENS_ML / (1000 * ENCRYPTED_BYTES_PER_TOKEN)
WATER_ML_PER_OUTPUT_BYTE = WATER_PER_1K_OUTPUT_TOKENS_ML / (1000 * ENCRYPTED_BYTES_PER_TOKEN)

API_PATTERNS = {
    "anthropic": ["api.anthropic.com"],
    "openai": ["api.openai.com"],
}

TOOL_AGENTS = {
    "opencode": ["opencode"],
    "claude-code": ["claude-code", "claude code"],
    "antigravity": ["antigravity", "anti-gravity", "antigravity-cli"],
}


def identify_tool(user_agent: str) -> str:
    ua = user_agent.lower()
    for tool, patterns in TOOL_AGENTS.items():
        for pat in patterns:
            if pat in ua:
                return tool
    return "unknown"


def identify_api(host: str) -> str:
    host = host.lower()
    for api, hosts in API_PATTERNS.items():
        for h in hosts:
            if h in host:
                return api
    return "other"


def estimate_water(
    bytes_sent: int, bytes_received: int, api: str = "other"
) -> tuple[float, int, int]:
    input_tokens = int(bytes_sent / ENCRYPTED_BYTES_PER_TOKEN)
    output_tokens = int(bytes_received / ENCRYPTED_BYTES_PER_TOKEN)

    water_ml = (
        input_tokens * WATER_PER_1K_INPUT_TOKENS_ML / 1000
        + output_tokens * WATER_PER_1K_OUTPUT_TOKENS_ML / 1000
    )

    return round(water_ml, 3), input_tokens, output_tokens


def format_water(ml: float) -> str:
    if ml < 1_000:
        return f"{ml:.1f} mL"
    litres = ml / 1000
    if litres < 1000:
        return f"{litres:.2f} L"
    return f"{litres / 1000:.2f} kL"

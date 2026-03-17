from pathlib import Path
env_path = Path('.env')
print('exists:', env_path.exists())
for line in env_path.read_text(encoding='utf-8').splitlines():
    if 'BINANCE_API_KEY' in line or 'BINANCE_SECRET' in line or 'DEMO' in line:
        k, _, v = line.partition('=')
        val = v.strip()
        start = repr(val[:4]) if val else 'EMPTY'
        has_q = val.startswith('"') or val.startswith("'")
        print(k.strip(), "=> length=" + str(len(val)), "| starts_with=" + start, "| has_quotes=" + str(has_q))

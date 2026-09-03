import os
from dotenv import load_dotenv

load_dotenv()
keys = os.getenv('TAVILY_API_KEYS', '')
print(f'TAVILY_API_KEYS present: {bool(keys)}')
key_list = [k.strip() for k in keys.split(",") if k.strip()]
print(f'Key count: {len(key_list)}')
if key_list:
    for i, key in enumerate(key_list):
        print(f'  Key {i}: {key[:30]}...')
else:
    print('Keys: <EMPTY>')

Add Gemini API keys to private vault

Recommended: run the helper script locally. Do NOT paste keys into chat.

Steps:
1. Revoke any keys accidentally posted in chat and reissue new keys.
2. On your machine run:
   VAULT_TOKEN=<your_vault_token> GEMINI_KEYS="key1,key2" ./scripts/update_vault_gemini_keys.sh

Notes:
- VAULT_TOKEN must be a fine-grained PAT with read/write access to the vault repo.
- The script writes secrets/gemini_keys.txt (one key per line) in the vault and pushes it.
- The CI workflow already reads secrets/gemini_keys.txt from the vault when it runs.
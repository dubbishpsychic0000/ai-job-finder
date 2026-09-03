# GitHub Actions Pipeline - Manual Trigger

Your career pipeline is **ready to run on GitHub** with all credentials configured!

## Quick Start

1. Go to: https://github.com/dubbishpsychic0000/ai-job-finder/actions/workflows/agent.yml

2. Click **"Run workflow"** button (top right)

3. Select:
   - Branch: **main** (default)
   - Email mode: **live** (sends real emails) or **draft** (creates Gmail drafts for review)
   - Click **"Run workflow"**

## What happens

✅ Clones your private vault (candidate data + OAuth secrets)
✅ Discovers jobs via Tavily web search + 15+ job board sources  
✅ Analyzes with AI scoring
✅ Applies safety gates (no invented claims, rate limits)
✅ Sends emails (gmail or draft mode)
✅ Sends WhatsApp status notification
✅ Syncs state back to vault

## Configuration

Current settings in `.env`:
- Email mode: **live** (real emails sent)
- Email from: **omar.benhamid@gmail.com**
- Max applications/day: **5**
- Tavily API keys: **7 rotating keys** (never hit rate limits)
- LLM: **disabled** (uses offline heuristics; enable with GEMINI_API_KEYS)

## Results

Last local run:
- **278 jobs discovered** (191 new)
- **108 flagged for investigation**
- **2 ready to apply**
- **0 emails sent** (waiting for your trigger)

## Troubleshooting

If the workflow fails:
1. Check logs: https://github.com/dubbishpsychic0000/ai-job-finder/actions
2. Look for:
   - `VAULT_TOKEN` secrets configured? 
   - `WHATSAPP_ACCESS_TOKEN` configured (optional)?
   - `GEMINI_API_KEYS` configured (optional)?

## WhatsApp Alerts

The pipeline sends WhatsApp status after each run. If you want test message:

1. Run workflow with "Send WhatsApp connection test" = ✓

2. You'll receive: "WhatsApp connection test: your Worldwide Career Agent is configured and ready."

---

**Ready?** Go to Actions → Click Run Workflow → Select **live** → Run!

#!/bin/bash
# Manual trigger script for GitHub Actions workflow
# Since gh workflow run requires admin, manually dispatch via curl instead

REPO="dubbishpsychic0000/ai-job-finder"
WORKFLOW="agent.yml"
PAT="${1:-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}"
EMAIL_MODE="${2:-live}"

echo "🚀 Triggering GitHub Actions workflow: $WORKFLOW"
echo "   Repository: $REPO"
echo "   Email Mode: $EMAIL_MODE"
echo ""

curl -X POST \
  -H "Accept: application/vnd.github.v3+json" \
  -H "Authorization: token $PAT" \
  "https://api.github.com/repos/$REPO/actions/workflows/$WORKFLOW/dispatches" \
  -d "{\"ref\": \"main\", \"inputs\": {\"email_mode\": \"$EMAIL_MODE\"}}"

echo ""
echo "✅ Workflow dispatch sent. Check: https://github.com/$REPO/actions"

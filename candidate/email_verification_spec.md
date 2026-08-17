# Email Verification Specification

## EmailVerificationService

Every opportunity must have a verified email before the system sends anything.

## Verification Hierarchy

1. **Explicit email in official employer posting**
   - Highest confidence — email found directly in the job listing on the company's own domain.

2. **Official company careers page**
   - Email found on the company's /careers, /jobs, or /recruitment page.

3. **Official company contact page**
   - Email found on the company's /contact or /contactez-nous page.

4. **Official recruitment page**
   - Email found on a dedicated recruitment subdomain or page.

5. **Trusted recruitment agency listing**
   - Email provided by a verified third-party recruitment platform (LinkedIn, Indeed, etc.) that links back to the employer.

6. **Other reliable source**
   - Any other independently verifiable source.

## Storage Fields

Each verified email is stored with:

```yaml
email: "recruitment@company.fr"
source_url: "https://company.fr/careers/job/123"
source_domain: "company.fr"
verified: true
verification_method: "explicit_in_posting"  # or "careers_page", "contact_page", etc.
verified_at: "2026-08-17T14:30:00Z"
confidence: 0.95  # 0.0 to 1.0 scale
```

## Rejection Rules

### DO NOT TRUST — Hard Rejections

The system must detect and reject:

- `example.com`
- `example.org`
- `example.net`
- `test.com`
- `test.org`
- `placeholder.com`
- `fake.com`
- `noreply.com`
- `no-reply.com`

### DO NOT INVENT — Forbidden Email Prefixes

The system must never fabricate:

- `hr@company.com`
- `jobs@company.com`
- `recruitment@company.com`
- `careers@company.com`
- `contact@company.com`
- `info@company.com`

**These prefixes are only valid if independently verified** (found in an official posting or page).

## Decision Flow

```
Is there a verified email?
  ├── YES → Send email (EMAIL method)
  └── NO → Is there an online application?
      ├── YES → Notify user: "Online application required, no email sent"
      └── NO → Notify user: "No verified email or online application found"
```

## Confidence Thresholds

- `confidence >= 0.9` — High confidence, auto-send email
- `confidence >= 0.7` — Medium confidence, create draft for review
- `confidence < 0.7` — Low confidence, do not send, notify user to apply manually

## Safety Gate

The WhatsApp notification system must NEVER bypass the Safety Gate. The email verification must always run before any email draft or send action is reported as completed.

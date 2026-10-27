# Product Specification — Multi-Agent PR Reviewer

> Status: Draft. Product implementation must not begin until this specification is approved through Genesis.

## Problem

Developers need reliable feedback on code changes before merging pull requests. Conventional automated checks may miss contextual logic defects, while AI-based reviewers can produce false positives, duplicate findings, or unsupported claims.

This product will automatically review GitHub pull requests using webhook-triggered processing, deterministic code-analysis tools, specialized AI agents, and a verification stage. It will produce actionable findings and publish review comments when the configured safeguards permit it.

The first release targets a working college project rather than a production-grade commercial service.

## Users

- Student developers who want automated feedback on their pull requests.
- Repository maintainers who want additional checks before merging changes.
- Project evaluators who need a demonstrable and testable AI-assisted code-review system.

## Functional requirements

- FR-1: The application shall expose an HTTP endpoint that receives GitHub pull-request webhook deliveries.
- FR-2: The application shall validate webhook signatures before accepting events for processing.
- FR-3: The application shall initiate a review when a pull request is opened or its source branch is updated.
- FR-4: The application shall reject unsupported events and actions without starting unnecessary reviews.
- FR-5: The application shall prevent duplicate webhook deliveries from unintentionally creating duplicate review jobs or comments.
- FR-6: The application shall retrieve pull-request metadata, changed files, diffs, and relevant source context through the GitHub API.
- FR-7: The first release shall provide Python code analysis using deterministic checks and AI-assisted analysis.
- FR-8: The architecture shall allow later support for JavaScript and TypeScript without rewriting the core review pipeline.
- FR-9: The review pipeline shall support separate responsibilities for security vulnerabilities, logic and code-quality defects, and testing gaps.
- FR-10: Each candidate finding shall include a description, severity, affected file, line reference when available, supporting evidence, and confidence assessment.
- FR-11: The system shall verify candidate findings against available source-code evidence and shall not present unsupported findings as confirmed defects.
- FR-12: The system shall consolidate duplicate findings and preserve relevant supporting evidence.
- FR-13: The application shall produce a structured review report and distinguish verified findings from uncertain observations.
- FR-14: The application shall support dry-run mode without publishing comments to GitHub.
- FR-15: The application shall publish actionable review comments only when publishing is explicitly enabled and the required GitHub permissions are available.
- FR-16: The system shall handle API failures, rate limits, malformed model responses, and processing errors without silently marking failed reviews as successful.
- FR-17: The application shall record review status and sufficient operational information to investigate failed or repeated jobs.
- FR-18: The system shall support a manual review command for testing, troubleshooting, and recovery.
- FR-19: The system shall provide tests for webhook authentication, event filtering, duplicate delivery handling, analysis, verification, deduplication, and publishing safeguards.

## Non-functional requirements

- NFR-1: The application shall run on Windows with Python 3.11 or a compatible supported Python version and shall not require a dedicated GPU.
- NFR-2: The webhook receiver shall use FastAPI and shall acknowledge accepted events promptly after validating and recording the review job.
- NFR-3: The system shall use a persistent job record so that accepted review requests can be recovered after an application restart.
- NFR-4: GitHub webhook secrets, GitHub credentials, and LLM API keys shall be supplied through environment variables or an appropriate secrets mechanism.
- NFR-5: Credentials shall not be committed to version control or exposed in application logs.
- NFR-6: GitHub webhook signatures shall be checked using a constant-time comparison.
- NFR-7: The initial deployment shall use the minimum GitHub permissions needed for reading pull requests and publishing comments.
- NFR-8: Dry-run execution shall not publish GitHub comments.
- NFR-9: Findings shall be traceable to available code evidence, and unverified candidates shall not be represented as confirmed defects.
- NFR-10: The system shall handle transient API errors and rate limits using bounded retries and clear failure states.
- NFR-11: LLM inference shall use a provider-independent interface, with Gemini as the initial provider when available and OpenRouter as an alternative.
- NFR-12: Automated tests shall not require live LLM API calls for ordinary deterministic regression checks.
- NFR-13: The system shall record processing duration, model configuration, and estimated API cost when available.
- NFR-14: The application shall not execute code submitted in pull requests by default.

## Constraints

- Development environment: Windows and PowerShell.
- Available hardware: 16 GB RAM without a dedicated GPU.
- Backend: Python and FastAPI.
- Initial language support: Python.
- Planned additional language support: JavaScript and TypeScript.
- Initial LLM provider: Gemini, subject to available quota; OpenRouter shall be supported as an alternative.
- GitHub integration: Webhooks and the GitHub API.
- Development testing: Local development with synthetic webhook payloads, followed by testing against a controlled GitHub repository.
- Public webhook delivery requires a reachable HTTPS endpoint or an appropriate development tunnel.
- The initial version shall avoid unnecessary infrastructure and paid services.
- External repository content shall be treated as untrusted input.

## Non-goals

- Automatically merging pull requests.
- Automatically modifying source files or pushing code fixes.
- Guaranteeing detection of every defect or vulnerability.
- Treating AI-generated findings as authoritative without verification.
- Supporting every programming language in the first release.
- Requiring a GPU or local large-language-model inference.
- Building a large distributed production platform for the initial college-project release.

## Acceptance criteria

- AC-1: A valid signed pull-request webhook event creates a review job.
- AC-2: An invalid or missing webhook signature is rejected, and no review job is created.
- AC-3: Unsupported webhook events do not trigger a review.
- AC-4: Repeated delivery of the same event does not unintentionally create duplicate reviews or comments.
- AC-5: A review job can retrieve the metadata and changed-file information for an accessible test pull request, or produce a clear error.
- AC-6: A controlled Python sample containing a supported defect produces a finding with an appropriate location and supporting evidence.
- AC-7: Unsupported or unsubstantiated candidate findings are not reported as confirmed defects.
- AC-8: Duplicate findings from multiple analysis components are consolidated.
- AC-9: Generated findings conform to the required report schema.
- AC-10: Dry-run mode generates a report without publishing GitHub comments.
- AC-11: Publishing safeguards prevent comments from being created when publishing is disabled.
- AC-12: Automated tests cover webhook authentication, event filtering, duplicate deliveries, verification, API errors, and publishing safeguards.
- AC-13: A failed review is recorded as failed or retryable rather than silently marked successful.
- AC-14: The application runs its core tests on the specified Windows environment without requiring a GPU.
- AC-15: A controlled end-to-end demonstration shows a pull-request event triggering analysis and producing a review report.

## Risks

- Risk: LLM provider quotas interrupt reviews.
  - Mitigation: Support multiple providers, bounded retries, clear failure states, and deterministic regression tests.

- Risk: Webhook spoofing or tampering causes unauthorized processing.
  - Mitigation: Validate the GitHub HMAC signature and store the webhook secret securely.

- Risk: Duplicate webhook deliveries create duplicate reviews.
  - Mitigation: Record delivery identifiers and enforce idempotent processing.

- Risk: AI agents generate redundant or unsupported findings.
  - Mitigation: Normalize findings, verify evidence, deduplicate results, and preserve uncertainty.

- Risk: A review fails after GitHub has already delivered the webhook.
  - Mitigation: Persist review jobs and provide recovery and retry mechanisms.

- Risk: Public webhook exposure creates a security vulnerability.
  - Mitigation: Validate requests, minimize permissions, restrict sensitive endpoints, and avoid exposing secrets.

- Risk: Untrusted code attempts to manipulate an analysis agent.
  - Mitigation: Treat repository content as data, isolate tool permissions, and avoid executing submitted code by default.

- Risk: Hosted inference exposes private source code to a third party.
  - Mitigation: Begin with non-sensitive test repositories, review provider data-handling terms, and avoid sending unnecessary repository context.

- Risk: Project complexity exceeds the available development time.
  - Mitigation: Implement and test the Python review engine first, then integrate webhooks and automatic publishing.

## Open questions

- Which exact Gemini model or OpenRouter model should be selected after checking available quota and cost?
- Which public or synthetic Python test corpus should be used for regression testing?
- Which hosting or development-tunnel option should expose the webhook endpoint during testing?
- What maximum review latency and model cost are acceptable?
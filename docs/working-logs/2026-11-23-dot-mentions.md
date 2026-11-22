# Dot mention polling

The mention bridge uses user-authorized search with an overlap and durable message receipts. The initial search records existing messages without waking Dot. Each later result must mention the authenticated user and must not be deleted or self-sent. A truncated page set fails the poll without advancing its watermark. Gateway delivery is retried from the same watermark on failure.

The bridge keeps its local state private; the polling process needs an external supervisor for restart after process or host failure. Existing event subscriptions and callbacks remain separate.

# Realtime investigation

Realtime triage classifies a message quickly, then continues investigation in the same ephemeral session when needed. The first card is reserved in SQLite before sending so an interrupted process does not send it twice. An ambiguous crash between reservation and send can lose a card; explicit send failures release the reservation. Retried investigations start a new ephemeral session and retain the first-card reservation. Both turns remain read-only and session cleanup runs in `finally`.

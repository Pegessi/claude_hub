# Feishu group binding recovery

A successful single-chat lookup can omit the chat ID in its response body. The
client now uses the ID from that request as a fallback while list items still
require their own IDs. A response without group form information remains
unusable, and private chats and existing binding conflicts remain rejected.

Group binding errors are displayed separately from the panel's general error
state so users can reload the selected Bot's groups and retry. Switching Bot or
successfully reloading clears the old group binding error. Tests use synthetic
responses; live post-fix binding was not performed.

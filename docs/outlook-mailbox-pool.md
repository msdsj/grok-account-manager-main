# Outlook Mailbox Pool

The Outlook option in the Grok registration task manages an existing mailbox
pool used to retrieve verification emails through Microsoft Graph or Outlook
IMAP. This feature only stores and consumes existing Outlook accounts; it does
not register Microsoft accounts automatically.

Use one account per line:

```text
email----password----clientId----refreshToken----auto
```

`password` remains in the format for compatibility with existing exports, but
Graph and IMAP OAuth access use `clientId` and `refreshToken`. The mode may be
`auto`, `imap`, or `graph`.

The local pool is written to `output/mailboxes/outlook-accounts.txt`; this path
is excluded from Git and is written with owner-only permissions where the
platform supports them. The browser API sends no-store response headers and
only permits local origins because the file contains credentials.

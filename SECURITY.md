# v4 Security / Deployment Notes

- No external API calls are made by the runtime application.
- No LLM is required.
- No inbound listening port is opened.
- No cloud database is required.
- The supplied portfolio CSV is bundled locally; runtime does not fetch GitHub.
- SQLite database, backups, invoice PDFs and CSV exports remain on locations selected by the user.
- Run with normal user privileges after installation.

Before enterprise production deployment:

1. Code-sign executable and installer.
2. Validate against corporate AppLocker/WDAC/Defender policy.
3. Restrict NTFS permissions on invoice/database/output directories.
4. Establish backup/retention policy for the SQLite database.
5. Confirm handling of real invoice/property data under corporate privacy and records policies.
6. Perform Windows 11 Enterprise DPI/accessibility/UAT.
7. Keep regression invoices sanitized or within approved controlled test storage.

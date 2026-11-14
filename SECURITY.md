# Security policy

## Supported versions

Security fixes are made for the latest release. Please upgrade before reporting.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | yes       |

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting instead: open the **Security** tab of
https://github.com/saizul/cloudpanel-backup and choose **Report a
vulnerability**.

Include what you found, how to reproduce it and what an attacker could do with
it. You should get an answer within a week. Once a fix is released you will be
credited in the changelog unless you prefer otherwise.

Never include real credentials, access keys, database dumps or backup archives
in a report. Replace domains with `example.com` and addresses with
documentation ranges (192.0.2.0/24, 2001:db8::/32).

## Scope

In scope:

- restore of a tampered backup: path traversal, symlink and hard link tricks,
  device files, setuid bits, checksum bypasses
- leaking secrets: passwords on command lines, in logs, in manifests or in
  files with loose permissions
- the S3 client (signing, TLS, handling of untrusted XML answers), the rsync
  and SSH command lines (argument or shell injection)
- the systemd units and cron lines written by the `schedule` command

Out of scope: the security of the storage provider, of the server's root
account, and of programs this tool calls (`mariadb-dump`, `age`, `gpg`,
`rsync`, `ssh`, `clpctl`).

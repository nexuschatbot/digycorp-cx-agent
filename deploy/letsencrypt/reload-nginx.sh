#!/bin/sh
# Installed to /etc/letsencrypt/renewal-hooks/deploy/ by scripts/remote-deploy.sh.
# Certbot runs every executable in that directory after a certificate is
# ACTUALLY renewed (not on a no-op renew check, and not on --dry-run).
#
# WHY THIS EXISTS. nginx reads `ssl_certificate` ONCE, at start or reload, and
# holds it in memory. `certbot renew` replaces the files under live/ and stops
# there: certbot.service on this box is a bare `certbot -q renew`, and we use
# `certonly` precisely so certbot never edits nginx (see docs/DEPLOYMENT.md §4).
# Nothing in that chain reloads the web server. Without this hook nginx serves
# the OLD certificate until something else reloads it — so the site would begin
# serving an EXPIRED certificate roughly 30 days after a successful renewal,
# with certbot reporting success the whole time.
#
# It was invisible until 2026-09-09 because a deploy reloads nginx and deploys
# were frequent enough to keep standing on it by accident. One quiet month
# between the renewal and the expiry is all it takes. Found while adding
# elite-group.digycorp.com to the certificate, not by anything failing.
#
# This is the same shape as every entry in DEPLOYMENT.md §5: two correct things
# — certbot renewing, nginx caching — disagreeing about who reloads.
set -e
systemctl reload nginx

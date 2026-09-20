#!/bin/bash
# Pin the intermediate certificates a host fails to send.
#   certs/fetch-chain.sh ftp.txdot.gov [another.host ...]
# Some servers (ftp.txdot.gov, Sep 2026) send only their own certificate, not
# the intermediate that links it to a root. Browsers quietly fetch the missing
# link from the URL inside the certificate (AIA); Node, which Playwright's
# download client runs on, does not — so every download from that host fails
# with "unable to verify the first certificate". This walks the AIA links from
# the server's certificate up to a self-signed root and appends every
# intermediate it finds to certs/extra-ca.pem, which archive_page.py hands to
# Node (NODE_EXTRA_CA_CERTS). Roots are not added; the standard store has them.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/extra-ca.pem"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for host in "$@"; do
    echo "== $host"
    openssl s_client -connect "$host:443" -servername "$host" </dev/null 2>/dev/null \
        | openssl x509 -outform PEM > "$TMP/cert.pem" || { echo "   could not fetch the server certificate"; continue; }
    for _ in 1 2 3 4 5; do
        subject=$(openssl x509 -in "$TMP/cert.pem" -noout -subject | sed 's/^subject= *//')
        issuer=$(openssl x509 -in "$TMP/cert.pem" -noout -issuer | sed 's/^issuer= *//')
        if [ "$subject" = "$issuer" ]; then echo "   root reached: $subject"; break; fi
        aia=$(openssl x509 -in "$TMP/cert.pem" -noout -text | grep -o 'CA Issuers - URI:[^ ]*' | head -1 | sed 's/CA Issuers - URI://')
        if [ -z "$aia" ]; then echo "   no CA Issuers URL in: $subject — cannot walk further"; break; fi
        curl -fsSL "$aia" -o "$TMP/issuer.der" || { echo "   could not download $aia"; break; }
        # AIA usually serves DER, occasionally PEM or PKCS#7
        openssl x509 -inform DER -in "$TMP/issuer.der" -out "$TMP/issuer.pem" 2>/dev/null \
            || openssl x509 -in "$TMP/issuer.der" -out "$TMP/issuer.pem" 2>/dev/null \
            || openssl pkcs7 -inform DER -in "$TMP/issuer.der" -print_certs -out "$TMP/issuer.pem" 2>/dev/null \
            || { echo "   unrecognised certificate format at $aia"; break; }
        isub=$(openssl x509 -in "$TMP/issuer.pem" -noout -subject | sed 's/^subject= *//')
        iiss=$(openssl x509 -in "$TMP/issuer.pem" -noout -issuer | sed 's/^issuer= *//')
        if [ "$isub" = "$iiss" ]; then echo "   root reached: $isub (not pinned)"; break; fi
        fp=$(openssl x509 -in "$TMP/issuer.pem" -noout -fingerprint -sha256)
        if [ -f "$OUT" ] && grep -q "$fp" "$OUT"; then
            echo "   already pinned: $isub"
        else
            { echo "# $isub"; echo "# issued by $iiss"; echo "# $fp"; echo "# added $(date +%Y-%m-%d) for $host"
              openssl x509 -in "$TMP/issuer.pem"; } >> "$OUT"
            echo "   pinned: $isub"
        fi
        cp "$TMP/issuer.pem" "$TMP/cert.pem"
    done
done
[ -f "$OUT" ] && echo "-> $OUT ($(grep -c 'BEGIN CERT' "$OUT") certificate(s)); takes effect on the next run"

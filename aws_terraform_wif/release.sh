#!/usr/bin/env bash
# Publish the standalone Terraform helper independently from StackSet templates.
set -euo pipefail
cd "$(dirname "$0")"

if [[ $# -lt 1 || $# -gt 2 || ( $# -eq 2 && "$2" != "--yes" ) ]]; then
    echo "Usage: $0 <S3_Bucket> [--yes]" >&2
    exit 1
fi
BUCKET="$1"
IFS= read -r VERSION < version.txt
KEY="aws-terraform-wif/${VERSION}/terraform_wif_bootstrap.py"
DIGEST=$(python3 -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("terraform_wif_bootstrap.py").read_bytes()).hexdigest())')
printf 'Publish s3://%s/%s\nSHA-256: %s\n' "$BUCKET" "$KEY" "$DIGEST"
if [[ "${2:-}" != "--yes" ]]; then
    read -r -p "Continue (y/n)? " answer
    [[ "$answer" == "y" ]] || exit 1
fi
# Atomic create-only publication; never overwrite an integrity-pinned release.
aws s3api put-object --bucket "$BUCKET" --key "$KEY" \
    --body terraform_wif_bootstrap.py --content-type text/x-python \
    --if-none-match '*' >/dev/null
printf 'https://%s.s3.amazonaws.com/%s\n' "$BUCKET" "$KEY"

#!/bin/bash
set -e

USER_POOL_ID=$1
EMAIL=${2:-admin@stratoclave.com}
if [ -z "$3" ]; then
  echo "[ERROR] Password is required as the third argument"
  echo "Usage: $0 <username> <email> <password> [group]"
  exit 1
fi
PASSWORD=$3

# ユーザー作成
aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$EMAIL" \
  --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
  --temporary-password "$PASSWORD" \
  --message-action SUPPRESS

# パスワードを恒久化
aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" \
  --username "$EMAIL" \
  --password "$PASSWORD" \
  --permanent

echo "User created: $EMAIL"

#!/usr/bin/env bash
# Scratch test of install.sh --check in a throwaway directory (no Docker, no network).
set -uo pipefail
SRC="$1"; T="$(mktemp -d)"; cp "$SRC/install.sh" "$SRC/.env.example" "$T/"; cd "$T"
pass=0; fail=0
ok()   { echo "  PASS $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL $1"; fail=$((fail+1)); }
get()  { grep -E "^$1=" .env | head -n1 | cut -d= -f2-; }
len()  { printf '%s' "$(get "$1")" | wc -c | tr -d ' '; }
set_() { K="$1" V="$2" awk 'BEGIN{k=ENVIRON["K"];v=ENVIRON["V"]} index($0,k"=")==1{print k"="v;d=1;next}{print} END{if(!d)print k"="v}' .env > .e && cat .e > .env && rm .e; }

echo "1) fresh run: creates .env, exits 2, lists what is missing"
out="$(./install.sh --check 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && ok "exit code 2" || bad "exit code $rc"
[[ "$out" == *"- BOT_TOKEN"* && "$out" == *"- GEMINI_API_KEY"* && "$out" == *"- GROQ_API_KEY"* ]] && ok "lists BOT_TOKEN/GEMINI/GROQ" || bad "missing list: $out"
[[ "$out" == *"still the local Azurite"* ]] && ok "flags the Azurite dev connection string" || bad "no Azurite warning"
[[ "$(get ENV)" == prod ]] && ok "ENV=prod" || bad "ENV=$(get ENV)"
[[ $(len POSTGRES_PASSWORD) -eq 48 ]] && ok "POSTGRES_PASSWORD generated (48 hex)" || bad "password len $(len POSTGRES_PASSWORD)"
[[ $(len PHONE_HASH_PEPPER) -eq 64 ]] && ok "PHONE_HASH_PEPPER generated (64 hex)" || bad "pepper len $(len PHONE_HASH_PEPPER)"
[[ "$(get DATABASE_URL)" == *":$(get POSTGRES_PASSWORD)@db:5432/montaj" ]] && ok "DATABASE_URL uses the generated password" || bad "DATABASE_URL mismatch"
[[ "$(stat -f %Lp .env 2>/dev/null || stat -c %a .env)" == 600 ]] && ok ".env is chmod 600" || bad ".env perms"
pw="$(get POSTGRES_PASSWORD)"; pep="$(get PHONE_HASH_PEPPER)"

echo "2) fill the required values, re-run"
set_ BOT_TOKEN 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw; set_ PUBLIC_DOMAIN bot.example.com; set_ TELEGRAM_API_ID 12345; set_ TELEGRAM_API_HASH abc
set_ AZURE_STORAGE_CONNECTION_STRING 'DefaultEndpointsProtocol=https;AccountName=montajprod1;AccountKey=SECRET==;EndpointSuffix=core.windows.net'
set_ GEMINI_API_KEY k; set_ GEMINI_ANALYSIS_MODEL m1; set_ GEMINI_PLANNER_MODEL m2; set_ GROQ_API_KEY g
out="$(./install.sh --check 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && ok "exit 0 when complete" || bad "exit $rc: $out"
[[ "$(get PUBLIC_BASE_URL)" == https://bot.example.com ]] && ok "PUBLIC_BASE_URL derived from the domain" || bad "PUBLIC_BASE_URL=$(get PUBLIC_BASE_URL)"
[[ "$(get AZURE_PUBLIC_BLOB_ENDPOINT)" == https://montajprod1.blob.core.windows.net ]] && ok "AZURE_PUBLIC_BLOB_ENDPOINT derived from the connection string" || bad "blob endpoint=$(get AZURE_PUBLIC_BLOB_ENDPOINT)"
[[ "$(get POSTGRES_PASSWORD)" == "$pw" && "$(get PHONE_HASH_PEPPER)" == "$pep" ]] && ok "secrets are stable across re-runs" || bad "secrets changed on re-run"
[[ "$(get AZURE_STORAGE_CONNECTION_STRING)" == *"AccountKey=SECRET==;EndpointSuffix"* ]] && ok "connection string with ';' and '=' survived intact" || bad "connection string mangled"

echo "3) error handling"
set_ BOT_TOKEN 123456:TEST
out="$(./install.sh --check 2>&1)"; rc=$?
[[ $rc -ne 0 && "$out" == *"does not look like a Telegram bot token"* ]] && ok "rejects a malformed BOT_TOKEN" || bad "malformed token accepted (rc=$rc)"
set_ BOT_TOKEN 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw
set_ PUBLIC_DOMAIN https://bot.example.com/
out="$(./install.sh --check 2>&1)"; rc=$?
[[ $rc -ne 0 && "$out" == *"bare host name"* ]] && ok "rejects a URL in PUBLIC_DOMAIN" || bad "accepted a URL (rc=$rc)"
set_ PUBLIC_DOMAIN bot.example.com
set_ DATABASE_URL 'postgresql+asyncpg://montaj:WRONG@db:5432/montaj'
out="$(./install.sh --check 2>&1)"; rc=$?
[[ $rc -ne 0 && "$out" == *"must match"* ]] && ok "rejects DATABASE_URL/POSTGRES_PASSWORD mismatch" || bad "mismatch accepted (rc=$rc)"
set_ DATABASE_URL "postgresql+asyncpg://montaj:${pw}@db:5432/montaj"
set_ LLM_PROVIDER azure; set_ GEMINI_API_KEY ''
out="$(./install.sh --check 2>&1)"; rc=$?
[[ $rc -eq 2 && "$out" == *"- AZURE_OPENAI_API_KEY"* && "$out" != *"- GEMINI_API_KEY"* ]] && ok "provider switch changes the required keys" || bad "provider switch (rc=$rc)"

echo; echo "passed=$pass failed=$fail"; rm -rf "$T"; [[ $fail -eq 0 ]]

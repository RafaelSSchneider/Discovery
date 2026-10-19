#!/usr/bin/env sh
# Uso: ./build.sh <MICROSOFT_APP_ID> <bot.seudominio.com.br>
# Gera assistente-teams.zip para upload no Teams (Apps > Gerenciar seus apps > Carregar um app).
set -eu
[ $# -eq 2 ] || { echo "Uso: $0 <MICROSOFT_APP_ID> <BOT_DOMAIN>"; exit 1; }
cd "$(dirname "$0")"
tmp=$(mktemp -d)
sed -e "s/{{MICROSOFT_APP_ID}}/$1/g" -e "s/{{BOT_DOMAIN}}/$2/g" manifest.json > "$tmp/manifest.json"
cp color.png outline.png "$tmp/"
rm -f assistente-teams.zip
(cd "$tmp" && zip -q -j - manifest.json color.png outline.png) > assistente-teams.zip
rm -rf "$tmp"
echo "✅ manifest/assistente-teams.zip gerado"

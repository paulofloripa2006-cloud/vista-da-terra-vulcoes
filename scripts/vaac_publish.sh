#!/usr/bin/env bash
# Vista da Terra — publica o JSON experimental do VAAC na branch vaac-data.
# Uso: vaac_publish.sh <pasta_checkout_vaac-data> <codigo_saida_do_coletor>
#
# Regras:
# - grava SOMENTE vaac_experimental.json, SOMENTE na branch vaac-data;
# - nunca usa force-push;
# - codigo 2 (falha critica do coletor, JSON nao gravado) = nada e publicado;
# - codigos 0 e 1 = publica o JSON valido (em 1 ele traz o erro registrado);
# - se outro commit entrou na vaac-data no meio, tenta rebase ate 3 vezes;
#   conflito = nao publica e sai com 3 (a proxima execucao refaz a coleta).
set -u

dir="$1"
code="$2"
file="vaac_experimental.json"

cd "$dir" || { echo "Pasta da vaac-data nao encontrada"; exit 4; }

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "vaac-data" ]; then
  echo "Recusado: checkout esta em '$branch', nao em vaac-data"
  exit 4
fi

if [ "$code" = "2" ]; then
  echo "Falha critica do coletor: nada foi publicado"
  exit 2
fi

if [ ! -f "$file" ]; then
  echo "Arquivo $file ausente: nada publicado"
  exit 4
fi

python3 -c "import json,sys; d=json.load(open('$file')); sys.exit(0 if d.get('schema')=='vaac-exp-1' else 1)" \
  || { echo "JSON invalido: nada publicado"; exit 4; }

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add -- "$file"
if git diff --cached --quiet; then
  echo "Sem mudancas no JSON"
  exit 0
fi

stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
git commit -q -m "VAAC experimental: coleta ${stamp} (saida do coletor ${code})" -- "$file"

for attempt in 1 2 3; do
  if git push -q origin HEAD:vaac-data; then
    echo "JSON publicado na vaac-data (tentativa ${attempt})"
    exit 0
  fi
  echo "Envio recusado (tentativa ${attempt}); atualizando a vaac-data"
  if ! git pull -q --rebase origin vaac-data; then
    git rebase --abort 2>/dev/null
    echo "Conflito com outro commit na vaac-data: nao publicado nesta execucao"
    exit 3
  fi
done

echo "Nao foi possivel publicar apos 3 tentativas"
exit 3

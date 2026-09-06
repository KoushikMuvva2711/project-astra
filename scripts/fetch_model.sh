#!/usr/bin/env bash
# Resumable GGUF fetch, built for an unreliable link.
#
# Why this exists: `ollama pull` restarts from zero when a TLS stream breaks,
# which makes it unusable here — the dev machine's Wi-Fi drops the connection
# every few seconds under sustained load (curl reports error 56, ollama reports
# "tls: bad record MAC"), so nothing larger than a few hundred MB ever finishes.
#
# curl -C - resumes from a byte offset, so a dropped link costs seconds rather
# than the whole file. The loop re-invokes curl until the byte count matches
# exactly. On a link that dies every ~13 MB, a 1.8 GB model needs well over a
# hundred attempts — hence the high cap.
#
# usage: fetch_model.sh URL DEST EXPECTED_BYTES [MAX_ATTEMPTS]
set -uo pipefail

URL="${1:?usage: fetch_model.sh URL DEST EXPECTED_BYTES [MAX_ATTEMPTS]}"
DEST="${2:?}"
EXPECTED="${3:?}"
MAX_ATTEMPTS="${4:-400}"

stalled=0

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  before=$(stat -c%s "$DEST" 2>/dev/null || echo 0)

  if [ "$before" -ge "$EXPECTED" ]; then
    echo "COMPLETE: $before bytes in $((attempt - 1)) attempts"
    exit 0
  fi

  curl -L -C - \
       --retry 5 --retry-delay 2 --retry-all-errors \
       --connect-timeout 30 \
       --speed-limit 30000 --speed-time 20 \
       -s -o "$DEST" "$URL" || true

  after=$(stat -c%s "$DEST" 2>/dev/null || echo 0)
  gained=$((after - before))
  echo "attempt $attempt: +$((gained / 1024)) KB -> $((after * 100 / EXPECTED))% ($after/$EXPECTED)"

  # Give up only if the link stops yielding anything at all for a long run;
  # slow progress is expected and fine.
  if [ "$gained" -le 0 ]; then
    stalled=$((stalled + 1))
    if [ "$stalled" -ge 25 ]; then
      echo "STALLED: 25 consecutive attempts with no progress at $after bytes"
      exit 1
    fi
    sleep 3
  else
    stalled=0
  fi
done

after=$(stat -c%s "$DEST" 2>/dev/null || echo 0)
echo "INCOMPLETE after $MAX_ATTEMPTS attempts: $after/$EXPECTED bytes"
exit 1

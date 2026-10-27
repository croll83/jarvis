#!/usr/bin/env bash
# Host unit tests for the pure duplex live session logic (no ESP-IDF needed).
# Builds the test as C and as C++ (main.cpp includes the header as C++).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/build"
INC="$ROOT/components/jarvis_ws_audio/include"
mkdir -p "$OUT"

gcc -std=c99 -Wall -Wextra -Werror -I"$INC" "$HERE/test_duplex_policy.c" -o "$OUT/test_duplex_policy_c"
g++ -x c++ -std=c++17 -Wall -Wextra -Werror -I"$INC" "$HERE/test_duplex_policy.c" -o "$OUT/test_duplex_policy_cpp"

echo "== C build =="
"$OUT/test_duplex_policy_c"
echo "== C++ build =="
"$OUT/test_duplex_policy_cpp"

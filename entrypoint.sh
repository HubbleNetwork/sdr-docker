#!/usr/bin/env bash
set -euo pipefail

FPGA_PATH="/opt/bladerf/hostedxA4.rbf"

if [ "${SDR_TYPE:-pluto}" = "bladerf" ]; then
    if bladeRF-cli -p 2>/dev/null | grep -q "bladeRF"; then
        if ! bladeRF-cli -e version 2>/dev/null | grep -q "FPGA size"; then
            echo "[entrypoint] bladeRF detected but FPGA not loaded — loading ${FPGA_PATH}..."
            bladeRF-cli -l "$FPGA_PATH" || {
                echo "[entrypoint] WARNING: FPGA load failed. The bladeRF may need a power cycle."
                echo "[entrypoint] Continuing anyway — the app will retry on connection."
            }
        else
            echo "[entrypoint] bladeRF FPGA already loaded."
        fi
    else
        echo "[entrypoint] No bladeRF detected on USB — skipping FPGA load."
    fi
fi

exec "$@"

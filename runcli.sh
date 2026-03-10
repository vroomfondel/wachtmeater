#!/bin/bash

export SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"



case "${1:-}" in
  monitor)
    shift
    python3 -m wachtmeater.cli monitor "$@"
    ;;
  watcher)
    shift
    python3 -m wachtmeater.cli watcher --skip-startup-test-call "$@"
    ;;
  call)
    shift
    python3 -m wachtmeater.cli call "$@"
    ;;
  deploy)
    shift
    python3 -m wachtmeater.cli deploy "$@"
    ;;
  *)
    echo "Usage: $0 {monitor|watcher|call|deploy}"
    echo "  monitor  - Run a single MEATER status check"
    echo "  watcher  - Run the persistent watcher event loop"
    echo "  call     - Trigger a SIP phone call (pass text to speak)"
    echo "  deploy   - Deploy/delete a MEATER watcher K8s Job (--meater-url required)"
    exit 1
    ;;
esac

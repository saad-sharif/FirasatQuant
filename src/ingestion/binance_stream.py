import json
import os

import websocket

from src.config import PROJECT_ROOT

BASE_URL = "wss://stream.binance.com:9443/stream"


class BinanceStreamIngestor:
    """Subscribes to one or more Binance combined-stream channels for a
    symbol and writes each stream's raw payloads to its own JSONL file."""

    def __init__(self, symbol: str, streams: list[str], data_dir: str):
        self.symbol = symbol.lower()
        self.streams = streams
        # Relative paths resolve against the caller's cwd, not this file's
        # location — anchor to PROJECT_ROOT so it doesn't matter whether
        # this runs from notebooks/, src/, or the project root.
        self.data_dir = (
            data_dir if os.path.isabs(data_dir) else os.path.join(PROJECT_ROOT, data_dir)
        )

        self.stream_names = [f"{self.symbol}@{s}" for s in streams]
        self.socket_url = f"{BASE_URL}?streams={'/'.join(self.stream_names)}"

        os.makedirs(self.data_dir, exist_ok=True)
        self.output_paths = {
            stream: os.path.join(self.data_dir, f"{self.symbol}_{stream}.jsonl")
            for stream in streams
        }

        self._file_handles = {}
        self._ws = None

    @classmethod
    def from_config(cls, config: dict) -> "BinanceStreamIngestor":
        ingestion_cfg = config["ingestion"]
        return cls(
            symbol=ingestion_cfg["symbol"],
            streams=ingestion_cfg["streams"],
            data_dir=ingestion_cfg["data_dir"],
        )

    def _open_files(self):
        self._file_handles = {
            stream: open(path, "a", buffering=1)
            for stream, path in self.output_paths.items()
        }

    def _close_files(self):
        for f in self._file_handles.values():
            f.close()
        self._file_handles = {}

    def _route_key(self, envelope: dict) -> str | None:
        stream_name = envelope.get("stream", "")
        suffix = stream_name.split("@", 1)[-1] if "@" in stream_name else None
        return suffix if suffix in self._file_handles else None

    def _on_message(self, ws, message):
        envelope = json.loads(message)
        key = self._route_key(envelope)
        if key is None:
            return
        self._file_handles[key].write(json.dumps(envelope["data"]) + "\n")

    def _on_error(self, ws, error):
        print(f"WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        print(f"Connection closed: {close_status_code} {close_msg}")
        self._close_files()

    def _on_open(self, ws):
        print(f"Connected. Streaming: {'/'.join(self.stream_names)}")

    def run(self):
        self._open_files()
        self._ws = websocket.WebSocketApp(
            self.socket_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        try:
            self._ws.run_forever()
        except KeyboardInterrupt:
            print("Interrupted, closing files...")
            self._close_files()

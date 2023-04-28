#!/usr/bin/env python3
"""Runs an Assist pipeline in a loop, executing voice commands and printing TTS response URLs."""
from __future__ import annotations

import argparse
import asyncio
import audioop
import logging
import sys
import threading
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)


@dataclass
class State:
    """Client state."""

    args: argparse.Namespace
    running: bool = True
    recording: bool = False
    audio_queue: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rate",
        required=True,
        type=int,
        help="Rate of input audio (hertz)",
    )
    parser.add_argument(
        "--width",
        required=True,
        type=int,
        help="Width of input audio samples (bytes)",
    )
    parser.add_argument(
        "--channels",
        required=True,
        type=int,
        help="Number of input audio channels",
    )
    parser.add_argument(
        "--samples-per-chunk",
        type=int,
        default=1024,
        help="Number of samples to read at a time from stdin",
    )
    #
    parser.add_argument("--token", required=True, help="HA auth token")
    parser.add_argument(
        "--pipeline", help="Name of HA pipeline to use (default: preferred)"
    )
    parser.add_argument(
        "--server", default="localhost:8123", help="host:port of HA server"
    )
    #
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print DEBUG messages to console",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    _LOGGER.debug(args)

    # Start reading raw audio from stdin
    state = State(args=args)
    audio_thread = threading.Thread(
        target=read_audio,
        args=(
            state,
            asyncio.get_running_loop(),
        ),
        daemon=True,
    )
    audio_thread.start()

    try:
        await loop_pipeline(state)
    except KeyboardInterrupt:
        pass
    finally:
        state.recording = False
        state.running = False
        audio_thread.join()


async def loop_pipeline(state: State) -> None:
    """Run pipeline in a loop, executing voice commands and printing TTS URLs."""
    args = state.args

    url = f"ws://{args.server}/api/websocket"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as websocket:
            _LOGGER.debug("Authenticating")
            msg = await websocket.receive_json()
            assert msg["type"] == "auth_required", msg

            await websocket.send_json(
                {
                    "type": "auth",
                    "access_token": args.token,
                }
            )

            msg = await websocket.receive_json()
            _LOGGER.debug(msg)
            assert msg["type"] == "auth_ok", msg
            _LOGGER.info("Authenticated")

            message_id = 1
            pipeline_id: Optional[str] = None
            if args.pipeline:
                # Get list of available pipelines and resolve name
                await websocket.send_json(
                    {
                        "type": "assist_pipeline/pipeline/list",
                        "id": message_id,
                    }
                )
                msg = await websocket.receive_json()
                _LOGGER.debug(msg)
                message_id += 1

                pipelines = msg["result"]["pipelines"]
                for pipeline in pipelines:
                    if pipeline["name"] == args.pipeline:
                        pipeline_id = pipeline["id"]
                        break

                if not pipeline_id:
                    raise ValueError(
                        f"No pipeline named {args.pipeline} in {pipelines}"
                    )

            # Pipeline loop
            while True:
                # Clear audio queue
                while not state.audio_queue.empty():
                    state.audio_queue.get_nowait()

                state.recording = True

                # Run pipeline
                _LOGGER.debug("Starting pipeline")
                pipeline_args = {
                    "type": "assist_pipeline/run",
                    "id": message_id,
                    "start_stage": "stt",
                    "end_stage": "tts",
                    "input": {
                        "sample_rate": 16000,
                    },
                }
                if pipeline_id:
                    pipeline_args["pipeline"] = pipeline_id
                await websocket.send_json(pipeline_args)
                message_id += 1

                msg = await websocket.receive_json()
                _LOGGER.debug(msg)
                assert msg["success"], "Pipeline failed to run"

                # Get handler id.
                # This is a single byte prefix that needs to be in every binary payload.
                msg = await websocket.receive_json()
                _LOGGER.debug(msg)
                handler_id = bytes(
                    [msg["event"]["data"]["runner_data"]["stt_binary_handler_id"]]
                )

                # Audio loop for single pipeline run
                receive_event_task = asyncio.create_task(websocket.receive_json())
                while True:
                    audio_chunk = await state.audio_queue.get()

                    # Prefix binary message with handler id
                    send_audio_task = asyncio.create_task(
                        websocket.send_bytes(handler_id + audio_chunk)
                    )
                    pending = {send_audio_task, receive_event_task}
                    done, pending = await asyncio.wait(
                        pending,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if receive_event_task in done:
                        event = receive_event_task.result()
                        _LOGGER.debug(event)
                        event_type = event["event"]["type"]
                        if event_type == "run-end":
                            state.recording = False
                            _LOGGER.debug("Pipeline finished")
                            break

                        if event_type == "error":
                            raise RuntimeError(event["event"]["data"]["message"])

                        if event_type == "tts-end":
                            # URL of text to speech audio response (relative to server)
                            tts_url = event["event"]["data"]["tts_output"]["url"]
                            print(tts_url)

                        receive_event_task = asyncio.create_task(
                            websocket.receive_json()
                        )

                    if send_audio_task not in done:
                        await send_audio_task


def read_audio(state: State, loop: asyncio.AbstractEventLoop) -> None:
    """Reads chunks of raw audio from standard input."""
    try:
        args = state.args
        bytes_per_chunk = args.samples_per_chunk * args.width * args.channels
        rate = args.rate
        width = args.width
        channels = args.channels
        ratecv_state = None

        _LOGGER.debug("Reading audio from stdin")

        while True:
            chunk = sys.stdin.buffer.read(bytes_per_chunk)
            if (not chunk) or (not state.running):
                # Signal other thread to stop
                state.audio_queue.put_nowait(bytes())
                break

            if state.recording:
                # Convert to 16Khz, 16-bit, mono
                if channels != 1:
                    chunk = audioop.tomono(chunk, width, 1.0, 1.0)

                if width != 2:
                    chunk = audioop.lin2lin(chunk, width, 2)

                if rate != 16000:
                    chunk, ratecv_state = audioop.ratecv(
                        chunk,
                        2,
                        1,
                        rate,
                        16000,
                        ratecv_state,
                    )

                # Pass converted audio to loop
                loop.call_soon_threadsafe(state.audio_queue.put_nowait, chunk)
    except Exception:
        _LOGGER.exception("Unexpected error reading audio")


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())

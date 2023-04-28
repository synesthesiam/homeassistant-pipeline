# Assist Pipeline Client

Basic websocket client for an [Assist pipeline](https://developers.home-assistant.io/docs/voice/pipelines/).

This client:

1. Authenticates with a Home Assistant server
2. Runs an [Assist](https://www.home-assistant.io/docs/assist) audio pipeline
3. Reads raw audio from standard input, sending it to Home Assistant
4. Prints the relative URL of the text to speech (TTS) response to the voice command
5. Repeats steps 2-4 until you quit with CTRL+C


## Running

``` sh
arecord -r 16000 -c 1 -f S16_LE -t raw | \
    python3 audio_to_audio.py --rate 16000 --width 2 --channels 1 --token '<HA_LONG_LIVED_ACCESS_TOKEN>'
```

See `python3 audio_to_audio.py --help` for more options.

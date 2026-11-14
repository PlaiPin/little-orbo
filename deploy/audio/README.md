# Live audio commissioning

GPT-Live uses simultaneous mono PCM16 input/output at 24 kHz by default. Start
with a headset or an existing hardware/OS echo-cancelled audio path. The runtime
does not install an echo canceller; `aec_qualified = true` records an operator's
acoustic qualification of the selected devices, not a software capability.

From the repository, list devices and test the exact input/output pair:

```sh
uv run --frozen --extra live python -m orbo_runtime.audio_check list
uv run --frozen --extra live python -m orbo_runtime.audio_check check --input 1 --output 2 --rate 24000
uv run --frozen --extra live python -m orbo_runtime.audio_check duplex --input 1 --output 2 --rate 24000 --seconds 4
```

Replace the device indices with the actual devices from `list`. Device names
may be used when unambiguous. On the Pi, use the installed `orbo-audio-check`
entry point in `/opt/orbo/.venv/bin/` instead of `uv run ...`.

`duplex` captures in memory while submitting silence for the first half and a
quiet 440 Hz probe for the second half. It reports baseline/probe input levels,
clipping, and stream frame counts, then closes the devices. It saves no audio
and calls no service. A stream status error fails the command; a silent input,
clipping, or a large level increase requires inspection of the selected audio
path. The aggregate level difference is not an echo-cancellation score: room
noise, external speech, latency, gain control and the microphone affect it.
The report never marks AEC or physical audibility as qualified.

At the intended speaker volume and robot placement, confirm that speech is
heard, user speech remains intelligible while output plays, and the assistant's
own output does not trigger new user turns. Test a deliberate interruption and
resume. Repeat after changing devices, audio processing, gain or placement.
Only then set the corresponding `input_device`, `output_device`, and
`aec_qualified` in the owner-edited [embodied profile](../profiles/embodied.toml).

The cloud-free probe verifies device operation. The credentialed Live E2E
runner verifies provider audio and delegation separately; neither substitutes
for observing the complete conversation on the actual robot.

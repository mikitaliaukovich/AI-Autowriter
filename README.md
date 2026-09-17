# AI Autowriter

A local voice stenographer for Microsoft Word. You dictate your book in Russian; it
transcribes, strips the disfluencies and thinking-aloud, applies Russian typographic
convention, and writes the result into your document at the right place — and it takes
spoken corrections against the text it can see.

Everything runs on your machine. No audio, text or document content leaves it.

```
 Word task pane  ──── wss://localhost:3000 ────  local service
   reads a context window around the caret          ffmpeg → Silero VAD → Whisper (CPU)
   applies structured edit operations                      ↓
   keeps an undo journal                            Ollama / qwen3:8b (GPU)
                                                           ↓
                                                    Russian typography → edit ops
```

## Quick start

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Then, **once**, in an elevated PowerShell (Word loads sideloaded add-ins from a UNC
path, so the folder has to be shared):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\create-share.ps1
```

Start the service and leave it running while you write:

```powershell
.\scripts\run.ps1
```

In Word: **Home → Add-ins → More Add-ins → SHARED FOLDER → AI Autowriter**.

Press **Ctrl+Alt+G** (or the button in the pane) and start talking.

## Check your microphone first

This is the one thing worth doing before you dictate a word, because a silent input
looks exactly like a broken program:

```bash
python scripts/check_audio.py
```

It records a few seconds, then reports the level of each channel, whether the voice
detector considers it speech, whether a complete utterance would be produced, and what
Whisper actually heard — ending with the exact `config.toml` lines to use.

**Why this matters here.** An audio interface presents two channels even when one
microphone is plugged into one of them. The default downmix averages the live channel
with a silent one, which throws away 6 dB and can drop the signal below the voice
detector's threshold. On this machine the mic sits on the **right** channel, 36 dB above
the left, so `config.toml` ships with:

```toml
[audio]
channel = "right"
gain_db = 0.0
```

While listening, the task pane shows a live level meter. If it stays at "тишина" while
you speak, the sound is not reaching the service and nothing downstream can help.

## How it works

**The context window.** The anchor is Word's own selection. The pane reads a few
paragraphs either side of the caret (6 back, 2 forward by default) and sends them
labelled `P-1`, `P0`, `P+1`. After each edit it selects the end of what was written, so
the window follows your dictation; click somewhere else and the window moves there. It
walks outwards from the caret rather than loading the document, so cost does not grow
with the length of the book.

**Edit operations, not prose.** The model returns operations addressed to those
paragraph ids — `append_to_paragraph`, `insert_paragraphs_after`, `replace_paragraph`,
`replace_in_paragraph`, `delete_paragraph`, `set_style`, `revert`. This is what makes
"перепиши последнее предложение" an exact edit instead of a fuzzy search across the
manuscript.

**Nothing is applied blind.** Every operation carries a hash of the paragraph as it was
when the window was read. If you typed something in the meantime, the whole batch is
rejected and recomputed against the document as it actually is.

**Fragment edits are resolved before the document is touched.** Because that hash
guarantees the paragraph is byte-for-byte what the model was shown, a
`replace_in_paragraph` is applied in Python and sent to Word as a plain paragraph
replacement. Word's own `search()` never sees it — which matters, because it caps
patterns at 255 characters and matches literally, so a fragment the model quoted back
with a hyphen instead of an em dash simply was not found and the batch aborted, losing
the sentence. Matching here tolerates dash, quote, ё/е, spacing and case substitutions,
and when a fragment genuinely cannot be placed the model is asked again with the reason
rather than the words being dropped.

**Pauses do not cost you a sentence.** People stop mid-thought, and the endpointer
cannot tell that from the end of one. Three things absorb it: a 900 ms silence window,
a prompt rule that treats a fragment as a continuation of the paragraph rather than a
new one, and merging any clips still queued into a single utterance so Whisper gets the
whole phrase.

**Undo is ours, not Word's.** Office.js cannot drive Word's undo stack, so every batch
records how to reverse itself. Say «отмени последнее» or use the button in the pane.

**Commands versus dictation.** A wake word (`ассистент`, `помощник`) always means an
instruction. A few unambiguous phrases work without it — «новый абзац», «новая глава»,
«стоп запись», «отмени последнее». Words that are also plausible dialogue («стоп»,
«назад», «не надо») deliberately require the wake word, so a line of your novel is never
mistaken for a command. Everything else is classified by the model from context.

**Typography is not left to the model.** Dashes, «ёлочки», ellipses and spacing are
applied deterministically afterwards, because a manuscript has to be consistent to the
character and models are not.

**Hallucination filtering.** On silence, Whisper emits subtitle boilerplate — in Russian
this is reliably «Продолжение следует…». Left alone it lands in your book. Transcripts
are screened against a phrase list, Whisper's own confidence, and degenerate repetition.

## Measured on this machine

Ryzen 5 9600X (6c/12t, AVX-512 VNNI) · RX 9070 XT 16 GB (ROCm) · 32 GB RAM.

| Stage | Cost | Notes |
|---|---|---|
| Endpointing | 600 ms | trailing silence before an utterance is closed |
| Whisper `large-v3-turbo`, int8, CPU | ~2.4 s | almost entirely fixed: Whisper encodes a 30 s window whatever the clip length |
| `qwen3:8b` via Ollama, GPU | ~0.9 s | p50 873 ms, p95 1034 ms over the fixture suite; 90 tok/s |
| Applying to Word | ~0.2 s | |

So roughly **4 s from the end of a sentence to text in the document**. Capture never
blocks on processing, so you can keep talking; the next utterance is transcribed while
the model is still writing the previous one.

Model choices, measured rather than assumed:

- **ASR.** `small` costs 697 ms and `medium` 1858 ms, against 2434 ms for
  `large-v3-turbo`. `medium` is only 24% faster than turbo but noticeably worse at
  Russian, so it is a poor trade; the real choice is `small` for speed or turbo for
  accuracy. Accuracy is worth more than a second in a manuscript, so turbo is the
  default. Beam size and thread count change almost nothing — this is encoder-bound.
- **LLM.** `qwen3:8b` scores 16/16 on the fixture suite. `qwen3:4b` scored 22/28 *and*
  was an order of magnitude slower under the JSON grammar, so it is not a useful
  fallback.
- There is no prebuilt Vulkan whisper.cpp for Windows, so GPU speech recognition would
  mean building it from source. The `whisper.cpp` backend is already wired up behind
  `asr.backend` if you do.

## Configuration

Everything lives in `config.toml`; restart the service to apply. The settings most worth
knowing:

| Setting | Meaning |
|---|---|
| `audio.device` | exact DirectShow name — `python -m service.audio.capture --list` |
| `audio.channel` | `mix` / `left` / `right`; see the microphone section above |
| `audio.gain_db` | applied after channel selection, for a quiet input |
| `vad.threshold` | lower it if speech is not detected; raise it if noise is |
| `vad.silence_ms` | how long a pause ends a sentence; raise it if thoughts get chopped |
| `asr.model` | `large-v3-turbo` (default) or `small` for speed |
| `llm.model` | any Ollama model; `qwen3:8b` by default |
| `context.before` / `after` | size of the window the model can see and address |
| `hotkey.bindings` | tried in order; the first one Windows grants wins |

`project.json` holds your book's character and place names. They are fed to Whisper as a
prompt and to the model as background — this is the single most effective way to stop
names drifting in spelling from one paragraph to the next.

## Tools

```powershell
.\scripts\stop.ps1                             # stop the service and clean up strays
```

```bash
python scripts/check_audio.py                  # diagnose the microphone path
python scripts/replay.py clip.wav              # replay a file through the LIVE pipeline
python scripts/dictate.py "..." --context "…"  # run one utterance through the LLM
python scripts/dictate.py --audio clip.wav     # run an audio file through ASR + LLM
python scripts/bench.py --models a,b --repeat 3  # score models against the fixtures
python -m pytest tests/ -q                     # unit tests
```

`https://localhost:3000/dev/harness.html` runs the task pane's Word logic against an
in-memory Office.js mock, and `/dev/pane.html` boots the real pane against that mock so
the UI can be exercised without Word.

`scripts/dictate.py --context` takes a compact window spec: paragraphs separated by `/`,
the caret marked with `|`.

```bash
python scripts/dictate.py --context "Мэри молчала. / Он вышел и|" "и замер на пороге точка"
```

## Troubleshooting

**The pane says "нет связи со службой".** The service is not running, or its certificate
is missing. Start `scripts\run.ps1` and open `https://localhost:3000/health` in Edge — it
should load with no certificate warning. If it warns, re-run
`npx office-addin-dev-certs install`.

**The add-in does not appear in Word.** Check `\\<COMPUTERNAME>\addin` is reachable in
Explorer, and that it contains `manifest.xml`. `scripts\create-share.ps1` verifies both
and compares the share against the catalog Word actually has registered.

**Dictation does nothing.** Two tools, in order:

`scripts/check_audio.py` covers everything up to the microphone. If it reports no
speech, the problem is before the software.

`scripts/replay.py clip.wav` covers everything after it. It runs a recording through the
real orchestrator, endpointer, ASR and LLM with only the microphone and Word stubbed
out, and its log says which stage swallowed the audio:

| What the log shows | What it means |
|---|---|
| no `utterance: N.N s` line | the endpointer never closed an utterance — a VAD problem |
| `suppressed transcript` | the hallucination filter dropped it |
| `Без правок` | the model decided to make no change |
| `Панель Word не подключена` | the task pane was not reachable |

A level meter showing healthy speech while nothing reaches the document points at the
VAD. That combination was a real bug: Silero v5 needs the previous chunk's last 64
samples prepended to each window, and without it the model returns near-zero for
everything rather than failing — so capture looked perfect and no utterance was ever
produced. `tests/test_vad.py` guards against it with a real speech recording.

**Text appears in the wrong place.** The window follows the caret. Click where you want
to write, wait for the pane's context panel to update, then dictate.

**An edit was rejected.** The paragraph changed between the model reading it and the edit
being applied — usually because you typed. It retries automatically once.

**Only one pane is driven at a time.** If both a real Word pane and a dev preview are
open, the Word pane wins; the preview still shows status but is never written through.

**Ctrl+C hangs, or the port is "already in use".** Run `scripts\stop.ps1`.

One service is two OS processes: the venv's `python.exe` is a launcher that runs the
real interpreter as a child. If a run ends badly the interpreter can survive holding the
port, the global hotkey and an ffmpeg capture process — so the next start fails, the
hotkey silently moves to a fallback combination, and the microphone stays claimed.

The hang itself is fixed. It came from interpreter teardown rather than the server:
`asyncio.run` joins its thread pool on exit, a Whisper transcription running there cannot
be interrupted, and Python waits up to five minutes for it (`THREAD_JOIN_TIMEOUT`). Since
Ctrl+C during dictation is exactly when a transcription is in flight, the terminal sat on
"Shutting down". The service now releases everything it owns — pane sockets, ffmpeg, the
hotkey — and then exits immediately rather than waiting on a thread with nothing left to
save. Ctrl+Break works too, and a second one forces the exit.

## Layout

```
addin/manifest.xml     the only file in the shared catalog folder
taskpane/              the add-in: plain ES modules, no build step
  src/                 context.js, apply.js, journal.js, ws.js, ui.js
  dev/                 Office.js mock, browser test harness, pane preview
service/               the local service
  audio/               ffmpeg capture, Silero VAD endpointing, Win32 hotkey
  asr/                 Whisper backends and hallucination filtering
  llm/                 Ollama client, Russian prompts, ops schema
  pipeline/            orchestrator, command parsing, typography, finalisation
scripts/               setup, run, diagnostics, benchmark
tests/                 pytest suite and the Russian fixture set
```

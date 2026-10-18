# Attachments Agent-facing text

Why each sentence of the Agent-facing text the Attachments domain owns exists: the note `PreparedImage.note` (`core/attachments/images.py`) that Chat adds to the path note of an image copy the converter changed. Rules for this file: `.vorch/workflows/domain-map-workflow.md` -> Agent-facing text.

| Text | Reason |
| --- | --- |
| `resized from ... to ... pixels to fit the destination limits` | The Model needs the shown copy's dimensions when interpreting image coordinates; reduction can be required by dimensions or encoded size. The surrounding note states that the original file is unchanged. |

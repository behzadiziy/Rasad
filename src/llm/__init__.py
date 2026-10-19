"""Provider-agnostic LLM layer.

`base` defines the `LLMClient` interface and its result types, `gemini` the
Google Gemini implementation, and `factory` picks the client from `config.yaml`.
"""

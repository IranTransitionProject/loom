Run the unit test suite (no infrastructure required):

```bash
uv run pytest tests/ -v -m "not integration and not deepeval"
```

For integration tests (needs NATS running on localhost:4222):
```bash
uv run pytest tests/ -v -m integration
```

# FLUX test fixtures

Tests create synthetic 256, 512 and 1024 square Pillow images in memory. They are not real FLUX inference evidence. Shared wire examples live in `contracts/fixtures/`; historical 1024 fixtures remain valid.

The operator's `smoke_test.py --size 512 --controlled-failure` runs first/warm real generation plus an explicitly injected dependency failure that verifies inference is not replayed. Only this opt-in smoke command loads the model.

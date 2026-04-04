# Lab 02 Setup

## Requirements

- Python 3.8 or higher
- Standard library only: `threading`, `time`, `statistics`, `queue`

## Verify Python Version

```bash
python3 --version
# Expected: Python 3.8.x or higher
```

## Running the Lab

1. Save the complete code from `README.md` as `lab02.py`
2. Run: `python3 lab02.py`
3. The lab takes approximately 60–90 seconds to complete all four scenarios

## Expected Runtime Breakdown

- Scenario 1 (No Pool): ~25 seconds (100 concurrent × 25ms each)
- Scenario 2 (Pool=1): ~30 seconds (100 requests × ~25ms, serialized by pool=1 except parallelism of 1)  
- Scenario 3 (Pool=10): ~15 seconds
- Scenario 4 (Pool=100): ~20 seconds

## Understanding the Simulation

The lab uses `time.sleep()` to simulate connection creation (15ms) and query execution (10ms). This is deliberately simplified — in production, the costs come from TCP handshakes and actual DB processing. The relative impact of pool sizing is the same.

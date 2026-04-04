# Lab 01 Setup

## Requirements

- Python 3.8 or higher
- No external packages required (uses `sqlite3`, `time`, `statistics` from standard library)

## Verify Python Version

```bash
python3 --version
# Expected: Python 3.8.x or higher
```

## Running the Lab

1. Save the complete code from `README.md` as `lab01.py`
2. Run: `python3 lab01.py`
3. Observe the output — query count and timing for each approach

## Expected Runtime

Under 5 seconds on any modern machine. The SQLite in-memory database eliminates network latency, so the benchmark is measuring pure query overhead.

## Troubleshooting

**`sqlite3` not found:** Install Python with its standard library. On Ubuntu: `sudo apt install python3`.

**Results show 0ms:** Your machine is very fast. The speedup ratios are still meaningful. Add the simulated RTT from Extension Exercise 1 to see realistic numbers.

Frozen copies used only to build older emotion.db files in tests (2.9 merge). Never imported by Rivia.
  step1/rivia_emotion.py  Step 1 as installed (rivia-hub main 7f23ad1); test_layers.py and test_dial.py open a Step 1 DB with it.
  2.1/rivia_emotion.py    the Step 2.1 draft; test_drift_limits.py opens a 2.1 DB with it.
They sit under tests/ so the Step 1 scans (test_changelog.py: nothing else writes the state; test_emotion_app.py)
don't count them; those scans look at the top-level folder only.

# linear fixtures

Hand-written GraphQL responses in the shape of Linear's public API docs (`issues` connection with `nodes` and
`pageInfo`), with generic names only (team `PROJ`, people `Dev One` and `Dev Two`). `test/test_watch_linear.py` plays
them through a fake `urlopen`; no request reaches the network.

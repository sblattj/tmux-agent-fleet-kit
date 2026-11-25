# whosaid fixtures

`decisions-standup.json` is a hand-written `whosaid decisions --json` result (generic topics and speakers).
`fake-whosaid` stands in for the CLI: it prints `<workspace>/<meeting>/decisions.json` and refuses any call without
`--engine local`, so the tests prove the adapter never asks for a model or the network.

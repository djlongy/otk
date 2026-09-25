# otk lab

Compose lab on one Docker host: low Quay with a docker.io pull-through cache, high Quay, NiFi per side, `data/diode` as the one-way link, and the high-side importer.

Needs Docker with compose, Python 3.11, about 16 GB for the containers, and internet access for docker.io.

```sh
lab/up.sh                                                  # ports 28081 28082 28090 28091
docker compose -f lab/compose.yaml run --rm otk-low pack   # one pack from lab/otk-low.toml
python3 lab/e2e.py                                         # first pack, delta, lost pack, retarget, guard
lab/down.sh                                                # removes containers, volumes and state
```

Every scenario in `e2e.py` prints PASS or FAIL against digests read back from docker.io and both registries.

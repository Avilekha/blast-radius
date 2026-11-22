# Windows PowerShell equivalent of `make sample` + `make diag`.
# Uses cmd.exe for the redirect: PowerShell's ">" writes UTF-16 and would break score.py.
$ErrorActionPreference = "Stop"
$img = "blast-radius-entry"
docker build --platform linux/amd64 --tag $img .
if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

$run = "docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m " +
       "--pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL " +
       "--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp " +
       "--volume `"$PWD\sample\commits:/data/commits:ro`" " +
       "--volume `"$PWD\sample\tests.json:/data/tests.json:ro`" $img > output.jsonl"
cmd /c $run
if ($LASTEXITCODE -ne 0) { throw "docker run failed" }

python score.py sample sample\answers.json output.jsonl
python diag.py sample output.jsonl

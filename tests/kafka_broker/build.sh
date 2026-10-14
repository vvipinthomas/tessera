#!/usr/bin/env sh
# Builds a small Kafka-protocol broker for the streaming tests (franz-go's kfake, with two fixes so
# librdkafka clients work: empty record sets instead of null, and the client-sent leader epoch is ignored
# like a real broker does). Needs Go 1.24+. Output: tests/kafka_broker/kfake
set -e
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
cd "$here"
cp main.go "$work/"
cd "$work"
go mod init kfake >/dev/null 2>&1
export GOTOOLCHAIN=local GOFLAGS=-mod=mod
go mod edit -replace golang.org/x/crypto=github.com/golang/crypto@v0.38.0
go get github.com/twmb/franz-go/pkg/kfake@7bf9e28d5ebd3b9c08cfbf6b8692642bc3885e44
src=$(go list -m -f '{{.Dir}}' github.com/twmb/franz-go/pkg/kfake)
cp -r "$src" ./kfakesrc && chmod -R u+w ./kfakesrc
(cd kfakesrc && patch -p0 < "$here/kfake.patch")
go mod edit -replace github.com/twmb/franz-go/pkg/kfake=./kfakesrc
go build -o kfake .
cp kfake "$here/kfake"
echo "built $here/kfake"

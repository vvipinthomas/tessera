package main

import (
	"fmt"
	"os"
	"os/signal"
	"strconv"

	"github.com/twmb/franz-go/pkg/kfake"
)

func main() {
	port := 19092
	if len(os.Args) > 1 {
		port, _ = strconv.Atoi(os.Args[1])
	}
	c, err := kfake.NewCluster(kfake.Ports(port), kfake.NumBrokers(1), kfake.SeedTopics(3, "orders", "clicks", "mixed"))
	if err != nil {
		panic(err)
	}
	fmt.Println("ready", c.ListenAddrs())
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, os.Interrupt)
	<-ch
	c.Close()
}

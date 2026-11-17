package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestLargeStdinDoesNotKeepDetachedChildAlive(t *testing.T) {
	token := fmt.Sprintf("test-%d-%d", os.Getpid(), time.Now().UnixNano())
	started := time.Now()
	res := run(token, request{
		Argv: []string{"/bin/bash", "-c", "setsid sleep 30 >/dev/null 2>&1 & echo $!"},
		Cwd:  "/tmp", Input: strings.Repeat("x", 2*1024*1024), Timeout: 2, Limit: 32768,
	})
	if res.ExitCode != 0 || res.TimedOut || time.Since(started) > 5*time.Second {
		t.Fatalf("command did not finish promptly: %+v", res)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(res.Stdout))
	if err != nil {
		t.Fatal(err)
	}
	if err = syscall.Kill(pid, 0); err != syscall.ESRCH {
		t.Fatalf("background child still exists: %d: %v", pid, err)
	}
}

// sandbox_exec supervises one Docker exec call, including orphan descendants.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type request struct {
	Argv    []string `json:"argv"`
	Cwd     string   `json:"cwd"`
	Input   string   `json:"input"`
	Timeout int      `json:"timeout"`
	Limit   int      `json:"limit"`
}
type result struct {
	ExitCode        int    `json:"exit_code"`
	Stdout          string `json:"stdout"`
	Stderr          string `json:"stderr"`
	TimedOut        bool   `json:"timed_out"`
	Cancelled       bool   `json:"cancelled"`
	StdoutTruncated bool   `json:"stdout_truncated"`
	StderrTruncated bool   `json:"stderr_truncated"`
}
type bounded struct {
	head, tail   []byte
	total, limit int
}

func (b *bounded) Write(p []byte) (int, error) {
	n := len(p)
	b.total += n
	keep := min(len(p), b.limit/2-len(b.head))
	b.head = append(b.head, p[:keep]...)
	p = p[keep:]
	// Keep at most the last half, without allocating proportional to output size.
	if len(p) >= b.limit/2 {
		b.tail = append(b.tail[:0], p[len(p)-b.limit/2:]...)
	} else {
		extra := len(b.tail) + len(p) - b.limit/2
		if extra > 0 {
			b.tail = b.tail[extra:]
		}
		b.tail = append(b.tail, p...)
	}
	return n, nil
}
func (b *bounded) text() string {
	marker := ""
	if b.total > b.limit {
		marker = "\n...[output truncated]...\n"
	}
	return strings.ToValidUTF8(string(b.head)+marker+string(b.tail), "�")
}
func exists(path string) bool { _, err := os.Stat(path); return err == nil }

// After the primary child exits, Linux reparents its surviving descendants to
// this subreaper. Kill and reap those too, including children that used setsid.
func reapDescendants() {
	for {
		children := map[int]bool{}
		files, _ := filepath.Glob(fmt.Sprintf("/proc/%d/task/*/children", os.Getpid()))
		for _, f := range files {
			data, _ := os.ReadFile(f)
			for _, s := range strings.Fields(string(data)) {
				pid, _ := strconv.Atoi(s)
				children[pid] = true
			}
		}
		if len(children) == 0 {
			return
		}
		for pid := range children {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
		for pid := range children {
			var status syscall.WaitStatus
			_, _ = syscall.Wait4(pid, &status, 0, nil)
		}
	}
}
func run(token string, req request) result {
	cancelPath := "/tmp/laxagent-exec-" + token + ".cancel"
	defer os.Remove(cancelPath)
	out, errout := &bounded{limit: req.Limit}, &bounded{limit: 32768}
	res := result{ExitCode: -1}
	if exists(cancelPath) {
		res.Cancelled = true
		return res
	}
	// PR_SET_CHILD_SUBREAPER ensures background grandchildren remain ours to reap.
	_, _, errno := syscall.RawSyscall6(syscall.SYS_PRCTL, 36, 1, 0, 0, 0, 0)
	if errno != 0 {
		res.Stderr = "Cannot supervise container processes"
		return res
	}
	cmd := exec.Command(req.Argv[0], req.Argv[1:]...)
	cmd.Dir = req.Cwd
	// A file avoids a stdin-copy goroutine blocking Wait when a detached child
	// inherits stdin but never consumes a large file-tool request.
	input, err := os.CreateTemp("/tmp", "laxagent-input-")
	if err != nil {
		res.Stderr = err.Error()
		return res
	}
	defer input.Close()
	os.Remove(input.Name())
	if _, err = input.WriteString(req.Input); err != nil {
		res.Stderr = err.Error()
		return res
	}
	if _, err = input.Seek(0, 0); err != nil {
		res.Stderr = err.Error()
		return res
	}
	cmd.Stdin = input
	// Use explicit pipes: cmd.Wait must not wait on output held by background children.
	stdoutR, stdoutW, err := os.Pipe()
	if err != nil {
		res.Stderr = err.Error()
		return res
	}
	defer stdoutR.Close()
	defer stdoutW.Close()
	stderrR, stderrW, err := os.Pipe()
	if err != nil {
		res.Stderr = err.Error()
		return res
	}
	defer stderrR.Close()
	defer stderrW.Close()
	cmd.Stdout = stdoutW
	cmd.Stderr = stderrW
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		stdoutW.Close()
		stderrW.Close()
		res.Stderr = err.Error()
		return res
	}
	stdoutW.Close()
	stderrW.Close()
	drained := make(chan bool, 2)
	copyOutput := func(f *os.File, b *bounded) {
		buf := make([]byte, 16384)
		for {
			n, e := f.Read(buf)
			if n > 0 {
				b.Write(buf[:n])
			}
			if e != nil {
				break
			}
		}
		drained <- true
	}
	go copyOutput(stdoutR, out)
	go copyOutput(stderrR, errout)
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	deadline := time.NewTimer(time.Duration(req.Timeout) * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	waiting := true
	for waiting {
		select {
		case <-done:
			waiting = false
		case <-deadline.C:
			res.TimedOut = true
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			<-done
			waiting = false
		case <-ticker.C:
			if exists(cancelPath) {
				res.Cancelled = true
				_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
				<-done
				waiting = false
			}
		}
	}
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	reapDescendants()
	<-drained
	<-drained
	res.ExitCode = cmd.ProcessState.ExitCode()
	res.Stdout = out.text()
	res.Stderr = errout.text()
	res.StdoutTruncated = out.total > out.limit
	res.StderrTruncated = errout.total > errout.limit
	return res
}
func main() {
	// Arguments and request are private server protocol, never model arguments.
	if os.Args[1] == "cancel" {
		if err := os.WriteFile("/tmp/laxagent-exec-"+os.Args[2]+".cancel", nil, 0600); err != nil {
			os.Exit(1)
		}
		return
	}
	var req request
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil {
		fmt.Fprintln(os.Stderr, "Invalid supervisor request")
		os.Exit(1)
	}
	json.NewEncoder(os.Stdout).Encode(run(os.Args[2], req))
}

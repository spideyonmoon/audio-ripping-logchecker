//go:build harness

package main

// CI-only checks (not part of the PR): run every sample log shipped with
// logchecker-go through the real library, then through sanitizeHTML, and
// verify that no library markup is lost and the visible text is unchanged.

import (
	"html"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/Nirzak/logchecker-go/logchecker"
)

var (
	reTag        = regexp.MustCompile(`<[^>]*>`)
	reLibraryTag = regexp.MustCompile(`</?(?:span|div|p|strong|em|br)\b[^>]*>`)
)

// visibleText of the library's raw output: only its own tags are markup;
// anything else (e.g. "<記号>" in a filename) is text.
func rawVisibleText(s string) string { return reLibraryTag.ReplaceAllString(s, "") }

func visibleText(s string) string { return html.UnescapeString(reTag.ReplaceAllString(s, "")) }

func TestLibraryMarkupSurvivesSanitizer(t *testing.T) {
	dir := os.Getenv("LOGS_DIR")
	if dir == "" {
		t.Skip("LOGS_DIR not set")
	}
	var files []string
	filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() && strings.Contains(filepath.ToSlash(p), "/originals/") {
			files = append(files, p)
		}
		return nil
	})
	if len(files) == 0 {
		t.Fatal("no sample logs found")
	}
	tags := 0
	for _, f := range files {
		lc := logchecker.New()
		if err := lc.NewFile(f); err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		lc.Parse()
		raw := lc.GetLog()
		out := sanitizeHTML(raw)
		rel, _ := filepath.Rel(dir, f)
		if containsMarkup(raw) && strings.Contains(out, "&lt;span") {
			t.Errorf("%s: library <span> markup was escaped by the sanitizer", rel)
		}
		for _, bad := range []string{"&lt;span", "&lt;/span", "&lt;strong", "&lt;br", "&lt;div"} {
			if strings.Contains(out, bad) {
				i := strings.Index(out, bad)
				t.Errorf("%s: sanitizer escaped library markup near %q", rel, out[max(0, i-80):min(len(out), i+80)])
				break
			}
		}
		if a, b := rawVisibleText(raw), visibleText(out); a != b {
			for i := 0; i < len(a) && i < len(b); i++ {
				if a[i] != b[i] {
					t.Errorf("%s: visible text changed by sanitizer at %d: raw %q vs out %q", rel, i, a[max(0, i-40):min(len(a), i+40)], b[max(0, i-40):min(len(b), i+40)])
					break
				}
			}
		}
		tags += len(reTag.FindAllString(out, -1))
		t.Logf("ok %-70s score=%d", rel, lc.GetScore())
	}
	t.Logf("%d logs, %d tags kept", len(files), tags)
}

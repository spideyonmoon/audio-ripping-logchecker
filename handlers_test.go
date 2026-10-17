package main

import (
	"bytes"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

const sampleLog = "Exact Audio Copy V1.6 from 23. October 2020\r\n\r\nEAC extraction logfile from 1. January 2024\r\n"

func testServer() *server {
	return &server{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
}

// multipartRequest builds a POST with an optional file part and text field.
func multipartRequest(t *testing.T, target, fileName string, file []byte, text *string) *http.Request {
	t.Helper()
	var body bytes.Buffer
	mw := multipart.NewWriter(&body)
	if fileName != "" {
		fw, err := mw.CreateFormFile(fieldFile, fileName)
		if err != nil {
			t.Fatal(err)
		}
		fw.Write(file)
	}
	if text != nil {
		mw.WriteField(fieldText, *text)
	}
	mw.Close()
	r := httptest.NewRequest(http.MethodPost, target, &body)
	r.Header.Set("Content-Type", mw.FormDataContentType())
	return r
}

func strPtr(s string) *string { return &s }

func TestReadLogInput(t *testing.T) {
	big := strings.Repeat("a", maxUploadBytes+1)
	cases := []struct {
		name     string
		req      func(t *testing.T) *http.Request
		wantErr  string // substring; "" means success
		wantName string
		wantRaw  string
	}{
		{
			name:     "file",
			req:      func(t *testing.T) *http.Request { return multipartRequest(t, "/", "rip.log", []byte(sampleLog), nil) },
			wantName: "rip.log", wantRaw: sampleLog,
		},
		{
			name:     "text",
			req:      func(t *testing.T) *http.Request { return multipartRequest(t, "/", "", nil, strPtr(sampleLog)) },
			wantName: pastedName, wantRaw: sampleLog,
		},
		{
			name: "text, urlencoded",
			req: func(t *testing.T) *http.Request {
				r := httptest.NewRequest(http.MethodPost, "/api", strings.NewReader(url.Values{fieldText: {sampleLog}}.Encode()))
				r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
				return r
			},
			wantName: pastedName, wantRaw: sampleLog,
		},
		{
			name: "text only in query string is ignored",
			req: func(t *testing.T) *http.Request {
				return multipartRequest(t, "/?"+url.Values{fieldText: {sampleLog}}.Encode(), "", nil, nil)
			},
			wantErr: "No log provided",
		},
		{
			name:    "both file and text",
			req:     func(t *testing.T) *http.Request { return multipartRequest(t, "/", "rip.log", []byte(sampleLog), strPtr(sampleLog)) },
			wantErr: "not both",
		},
		{
			name:    "neither",
			req:     func(t *testing.T) *http.Request { return multipartRequest(t, "/", "", nil, nil) },
			wantErr: "No log provided",
		},
		{
			name:    "whitespace-only text",
			req:     func(t *testing.T) *http.Request { return multipartRequest(t, "/", "", nil, strPtr(" \r\n\t ")) },
			wantErr: "No log provided",
		},
		{
			name:    "text too large",
			req:     func(t *testing.T) *http.Request { return multipartRequest(t, "/", "", nil, &big) },
			wantErr: errMsgTooLarge,
		},
		{
			name:    "file too large",
			req:     func(t *testing.T) *http.Request { return multipartRequest(t, "/", "rip.log", []byte(big), nil) },
			wantErr: errMsgTooLarge,
		},
		{
			name: "body far over the limit",
			req: func(t *testing.T) *http.Request {
				huge := strings.Repeat("a", 4*maxUploadBytes)
				return multipartRequest(t, "/", "", nil, &huge)
			},
			wantErr: errMsgTooLarge,
		},
		{
			name:    "disallowed file type",
			req:     func(t *testing.T) *http.Request { return multipartRequest(t, "/", "rip.html", []byte(sampleLog), nil) },
			wantErr: "File type not allowed",
		},
		{
			name:    "invalid UTF-8 text",
			req:     func(t *testing.T) *http.Request { return multipartRequest(t, "/", "", nil, strPtr("Exact Audio Copy \xff\xfe")) },
			wantErr: "not valid UTF-8",
		},
		{
			name: "markup in text",
			req: func(t *testing.T) *http.Request {
				return multipartRequest(t, "/", "", nil, strPtr(sampleLog+`<span class="good">Copy OK</span>`))
			},
			wantErr: "HTML markup",
		},
		{
			name: "markup in file",
			req: func(t *testing.T) *http.Request {
				return multipartRequest(t, "/", "rip.log", []byte(sampleLog+`<span class="good">Copy OK</span>`), nil)
			},
			wantErr: "HTML markup",
		},
		{
			name: "markup in UTF-16 file",
			req: func(t *testing.T) *http.Request {
				return multipartRequest(t, "/", "rip.log", utf16le(sampleLog+"<br>"), nil)
			},
			wantErr: "HTML markup",
		},
	}

	s := testServer()
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			in, errMsg := s.readLogInput(httptest.NewRecorder(), c.req(t), "test")
			if c.wantErr != "" {
				if !strings.Contains(errMsg, c.wantErr) {
					t.Fatalf("error = %q, want it to contain %q", errMsg, c.wantErr)
				}
				return
			}
			if errMsg != "" {
				t.Fatalf("unexpected error %q", errMsg)
			}
			if in.name != c.wantName || string(in.raw) != c.wantRaw || in.ext != ".log" {
				t.Fatalf("got name=%q ext=%q raw=%q", in.name, in.ext, in.raw)
			}
		})
	}
}

func TestCleanPastedText(t *testing.T) {
	cases := []struct{ in, want string }{
		{"line 1\r\nline 2\n\tx", "line 1\r\nline 2\n\tx"},
		{"\uFEFFExact Audio Copy", "Exact Audio Copy"},
		{"a\x00b\x07c\x1bd\x7fe\u0085f", "abcdef"},
		{"Track \u202EA\u202C \u2066B\u2069 \u200FC", "Track A B C"},
		{"日本語 Ñandú — ok", "日本語 Ñandú — ok"},
	}
	for _, c := range cases {
		got, ok := cleanPastedText(c.in)
		if !ok || got != c.want {
			t.Errorf("cleanPastedText(%q) = %q, %v; want %q", c.in, got, ok, c.want)
		}
	}
	if _, ok := cleanPastedText("bad \xc3\x28"); ok {
		t.Error("cleanPastedText accepted invalid UTF-8")
	}
}

func utf16le(s string) []byte {
	out := []byte{0xff, 0xfe}
	for _, r := range s { // ASCII-only test input
		out = append(out, byte(r), 0)
	}
	return out
}

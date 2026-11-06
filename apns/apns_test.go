package apns

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/sideshow/apns2"
)

func testCredentials(t *testing.T) string {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	keyPEM := string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}))
	t.Setenv("BARK_APNS_KEY_PATH", "")
	t.Setenv("BARK_APNS_KEY", keyPEM)
	t.Setenv("BARK_APNS_TOPIC", " cc.test.bark \n")
	t.Setenv("BARK_APNS_KEY_ID", " TESTKEY \n")
	t.Setenv("BARK_APNS_TEAM_ID", " TESTTEAM \n")
	t.Setenv("BARK_APNS_ENV", "production")
	previous := activeProvider.Load()
	t.Cleanup(func() { activeProvider.Store(previous) })
	return keyPEM
}

func TestCredentialsAndReload(t *testing.T) {
	key := testCredentials(t)
	for _, env := range []string{"", "production", " sandbox ", "DEVELOPMENT"} {
		t.Setenv("BARK_APNS_ENV", env)
		if err := ReCreateAPNS(2); err != nil {
			t.Fatal(err)
		}
		p := activeProvider.Load()
		wantHost := apns2.HostProduction
		if strings.Contains(strings.ToLower(env), "sandbox") || env == "DEVELOPMENT" {
			wantHost = apns2.HostDevelopment
		}
		if p.topic != "cc.test.bark" || p.clients[0].Host != wantHost || p.clients[0].Token.KeyID != "TESTKEY" || p.clients[0].Token.TeamID != "TESTTEAM" {
			t.Fatal("credentials were not normalized atomically")
		}
	}
	for _, variable := range []string{"BARK_APNS_TOPIC", "BARK_APNS_KEY_ID", "BARK_APNS_TEAM_ID", "BARK_APNS_KEY"} {
		t.Run(variable, func(t *testing.T) {
			old := os.Getenv(variable)
			t.Setenv(variable, " ")
			previous := activeProvider.Load()
			if err := ReCreateAPNS(1); err == nil {
				t.Fatal("missing credentials accepted on reload")
			}
			if activeProvider.Load() != previous {
				t.Fatal("failed reload replaced working provider")
			}
			t.Setenv(variable, old)
		})
	}
	t.Setenv("BARK_APNS_ENV", "private-value-not-to-log")
	if err := ReCreateAPNS(1); err == nil || strings.Contains(err.Error(), "private-value-not-to-log") {
		t.Fatal("invalid environment not safely rejected")
	}
	t.Setenv("BARK_APNS_ENV", "production")
	t.Setenv("BARK_APNS_KEY_PATH", filepath.Join(t.TempDir(), "absent.p8"))
	if err := ReCreateAPNS(1); err == nil {
		t.Fatal("missing file fell back to inline key")
	}
	path := filepath.Join(t.TempDir(), "key.p8")
	if err := os.WriteFile(path, []byte(key), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("BARK_APNS_KEY_PATH", " "+path+" \n")
	if err := ReCreateAPNS(1); err != nil {
		t.Fatal(err)
	}
	if err := ReCreateAPNS(0); err == nil {
		t.Fatal("zero clients accepted")
	}
}

func TestUnconfiguredProviderReturnsWithoutBlocking(t *testing.T) {
	previous := activeProvider.Swap(nil)
	defer activeProvider.Store(previous)
	if code, err := Push(&PushMessage{}); code != 503 || err == nil {
		t.Fatalf("got %d %v", code, err)
	}
}

func TestAPNSWireContract(t *testing.T) {
	testCredentials(t)
	if err := ReCreateAPNS(1); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name   string
		delete bool
		code   int
		reason string
	}{
		{"rich", false, 200, ""},
		{"delete", true, 200, ""},
		{"wrong-topic", false, 400, "DeviceTokenNotForTopic"},
		{"wrong-environment", false, 400, "BadDeviceToken"},
		{"expired-registration", false, 410, "Unregistered"},
		{"bad-provider", false, 403, "InvalidProviderToken"},
		{"oversized", false, 413, "PayloadTooLarge"},
		{"throttled", false, 429, "TooManyRequests"},
		{"unavailable", false, 503, "ServiceUnavailable"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.ProtoMajor != 2 || r.Method != http.MethodPost || r.URL.Path != "/3/device/offline-token" {
					t.Errorf("unexpected APNs request")
				}
				if r.Header.Get("apns-topic") != "cc.test.bark" || !strings.HasPrefix(r.Header.Get("authorization"), "bearer ") {
					t.Error("missing APNs authentication headers")
				}
				wantType, wantPriority := "alert", "10"
				if tc.delete {
					wantType, wantPriority = "background", "5"
				}
				if r.Header.Get("apns-push-type") != wantType || r.Header.Get("apns-priority") != wantPriority || r.Header.Get("apns-collapse-id") != "test-id" {
					t.Error("incorrect APNs delivery headers")
				}
				var body map[string]interface{}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				aps, ok := body["aps"].(map[string]interface{})
				if !ok {
					t.Error("missing aps")
					return
				}
				if tc.delete {
					if aps["content-available"] != float64(1) || aps["alert"] != nil || aps["sound"] != nil {
						t.Error("delete is not a silent notification")
					}
				} else if aps["mutable-content"] != float64(1) || aps["thread-id"] != "42" || body["ciphertext"] != "opaque-fixture" || body["isarchive"] != "1" || body["iv"] != "AAECAwQFBgcICQoL" || body["iv_encoding"] != "base64" {
					t.Error("extension/archiving parameters were lost")
				}
				w.WriteHeader(tc.code)
				if tc.code != 200 {
					_ = json.NewEncoder(w).Encode(map[string]string{"reason": tc.reason})
				}
			}))
			server.EnableHTTP2 = true
			server.StartTLS()
			defer server.Close()
			p := activeProvider.Load()
			p.clients[0].Host, p.clients[0].HTTPClient = server.URL, server.Client()
			msg := &PushMessage{Id: "test-id", DeviceToken: "offline-token", Title: "test", Body: "body", Sound: "bell.caf", ExtParams: map[string]interface{}{"group": 42, "ciphertext": "opaque-fixture", "isarchive": 1, "iv": "AAECAwQFBgcICQoL", "iv_encoding": "base64"}}
			if tc.delete {
				msg.ExtParams["delete"] = "1"
			}
			code, err := Push(msg)
			if code != tc.code || (tc.code == 200 && err != nil) || (tc.code != 200 && (err == nil || err.Error() != tc.reason)) {
				t.Fatalf("got %d %v", code, err)
			}
		})
	}
}

func TestConcurrentPushAndReload(t *testing.T) {
	testCredentials(t)
	if err := ReCreateAPNS(1); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer server.Close()
	newFixture := func() *provider {
		return &provider{topic: "cc.test.bark", clients: []*apns2.Client{{Host: server.URL, HTTPClient: server.Client()}}}
	}
	activeProvider.Store(newFixture())
	var wg sync.WaitGroup
	for range 20 {
		wg.Go(func() {
			for range 10 {
				if code, err := Push(&PushMessage{DeviceToken: "offline-token", Body: "body"}); code != 200 || err != nil {
					t.Errorf("got %d %v", code, err)
				}
			}
		})
	}
	for range 20 {
		activeProvider.Store(newFixture())
	}
	wg.Wait()
}

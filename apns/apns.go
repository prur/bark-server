package apns

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync/atomic"
	"time"

	"github.com/mritd/logger"
	"github.com/sideshow/apns2"
	"github.com/sideshow/apns2/payload"
	"github.com/sideshow/apns2/token"
	"golang.org/x/net/http2"
)

type PushMessage struct {
	Id          string `form:"id,omitempty" json:"id,omitempty" xml:"id,omitempty" query:"id,omitempty"`
	DeviceToken string `form:"-" json:"-" xml:"-" query:"-"`
	DeviceKey   string `form:"device_key,omitempty" json:"device_key,omitempty" xml:"device_key,omitempty" query:"device_key,omitempty"`
	Subtitle    string `form:"subtitle,omitempty" json:"subtitle,omitempty" xml:"subtitle,omitempty" query:"subtitle,omitempty"`
	Title       string `form:"title,omitempty" json:"title,omitempty" xml:"title,omitempty" query:"title,omitempty"`
	Body        string `form:"body,omitempty" json:"body,omitempty" xml:"body,omitempty" query:"body,omitempty"`
	// ios notification sound(system sound please refer to http://iphonedevwiki.net/index.php/AudioServices)
	Sound     string                 `form:"sound,omitempty" json:"sound,omitempty" xml:"sound,omitempty" query:"sound,omitempty"`
	ExtParams map[string]interface{} `form:"ext_params,omitempty" json:"ext_params,omitempty" xml:"ext_params,omitempty" query:"ext_params,omitempty"`
}

// Check if it's an empty message, empty messages might be silent push notifications
func (p PushMessage) IsEmptyAlert() bool {
	return p.Title == "" && p.Body == "" && p.Subtitle == ""
}

func (p PushMessage) IsDelete() bool {
	val := p.ExtParams["delete"]
	return val == "1" || val == 1 || val == 1.0
}

const (
	PayloadMaximum = 4096
)

type provider struct {
	topic   string
	clients []*apns2.Client
	next    atomic.Uint64
}

// Publish credentials and clients together only after complete validation.
var activeProvider atomic.Pointer[provider]

func ReCreateAPNS(maxClientCount int) error {
	if maxClientCount < 1 {
		return fmt.Errorf("invalid number of clients")
	}

	var keyBytes []byte
	envKeyPath := strings.TrimSpace(os.Getenv("BARK_APNS_KEY_PATH"))
	envKey := os.Getenv("BARK_APNS_KEY")

	if envKeyPath != "" {
		b, err := os.ReadFile(envKeyPath)
		if err != nil {
			return fmt.Errorf("failed to read APNS key file: %w", err)
		}
		keyBytes = b
	} else if envKey != "" {
		keyBytes = []byte(envKey)
	} else {
		// No fallback to internal testing key if any credentials are being configured
		return fmt.Errorf("no APNS key provided: must set BARK_APNS_KEY_PATH or BARK_APNS_KEY")
	}

	topic := strings.TrimSpace(os.Getenv("BARK_APNS_TOPIC"))
	keyID := strings.TrimSpace(os.Getenv("BARK_APNS_KEY_ID"))
	teamID := strings.TrimSpace(os.Getenv("BARK_APNS_TEAM_ID"))

	if topic == "" || keyID == "" || teamID == "" {
		var missing []string
		if topic == "" {
			missing = append(missing, "BARK_APNS_TOPIC")
		}
		if keyID == "" {
			missing = append(missing, "BARK_APNS_KEY_ID")
		}
		if teamID == "" {
			missing = append(missing, "BARK_APNS_TEAM_ID")
		}
		return fmt.Errorf("missing required APNS credentials: %s", strings.Join(missing, ", "))
	}

	authKey, err := token.AuthKeyFromBytes(keyBytes)
	if err != nil {
		return fmt.Errorf("failed to create APNS auth key: %w", err)
	}

	var rootCAs *x509.CertPool
	if runtime.GOOS == "windows" {
		rootCAs = x509.NewCertPool()
	} else {
		rootCAs, err = x509.SystemCertPool()
		if err != nil {
			return fmt.Errorf("failed to get rootCAs: %w", err)
		}
	}

	for _, ca := range apnsCAs {
		rootCAs.AppendCertsFromPEM([]byte(ca))
	}

	host := apns2.HostProduction
	if envEnv := strings.TrimSpace(os.Getenv("BARK_APNS_ENV")); envEnv != "" {
		if strings.EqualFold(envEnv, "development") || strings.EqualFold(envEnv, "sandbox") {
			host = apns2.HostDevelopment
		} else if strings.EqualFold(envEnv, "production") {
			host = apns2.HostProduction
		} else {
			return fmt.Errorf("invalid BARK_APNS_ENV: must be 'production', 'sandbox', or 'development'")
		}
	}

	p := &provider{topic: topic}
	for i := 0; i < min(runtime.NumCPU(), maxClientCount); i++ {
		client := &apns2.Client{
			Token: &token.Token{
				AuthKey: authKey,
				KeyID:   keyID,
				TeamID:  teamID,
			},
			HTTPClient: &http.Client{
				Transport: &http2.Transport{
					DialTLS: apns2.DialTLS,
					TLSClientConfig: &tls.Config{
						RootCAs: rootCAs,
					},
				},
				Timeout: apns2.HTTPClientTimeout,
			},
			Host: host,
		}
		logger.Infof("create apns client: %d (host: %s)", i, host)
		p.clients = append(p.clients, client)
	}

	activeProvider.Store(p)
	logger.Info("init apns client success...")
	return nil
}

func Push(msg *PushMessage) (code int, err error) {
	p := activeProvider.Load()
	if p == nil {
		return 503, fmt.Errorf("APNS provider is not configured")
	}
	pl := payload.NewPayload().MutableContent()
	pushType := apns2.PushTypeAlert
	priority := apns2.PriorityHigh
	if msg.IsDelete() {
		// Silent push notification
		pl = pl.ContentAvailable()
		pushType = apns2.PushTypeBackground
		priority = apns2.PriorityLow
	} else {
		// Regular push notification
		pl = pl.AlertTitle(msg.Title).
			AlertSubtitle(msg.Subtitle).
			AlertBody(msg.Body).
			Sound(msg.Sound).
			Category("myNotificationCategory")
		group, exist := msg.ExtParams["group"]
		if exist && group != nil {
			pl = pl.ThreadID(fmt.Sprintf("%v", group))
		}
	}

	for k, v := range msg.ExtParams {
		// Change all parameter names to lowercase to prevent inconsistent capitalization
		pl.Custom(strings.ToLower(k), fmt.Sprintf("%v", v))
	}

	client := p.clients[(p.next.Add(1)-1)%uint64(len(p.clients))]

	resp, err := client.Push(&apns2.Notification{
		CollapseID:  msg.Id,
		DeviceToken: msg.DeviceToken,
		Topic:       p.topic,
		Payload:     pl,
		Expiration:  time.Now().Add(24 * time.Hour),
		PushType:    pushType,
		Priority:    priority,
	})
	if err != nil {
		return 500, err
	}
	if resp.StatusCode != 200 {
		return resp.StatusCode, fmt.Errorf("%s", resp.Reason)
	}
	return 200, nil
}

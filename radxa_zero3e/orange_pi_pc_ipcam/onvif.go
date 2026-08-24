package main

import (
	"bytes"
	"crypto/rand"
	"crypto/sha1"
	"encoding/base64"
	"encoding/xml"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

type onvifClient struct {
	mu            sync.Mutex
	deviceService string
	username      string
	password      string
	ptzURL        string
	mediaURL      string
	profileToken  string
	http          *http.Client
}

func newONVIF(c config) *onvifClient {
	return &onvifClient{
		deviceService: strings.TrimSpace(c.ONVIFDeviceService),
		username: c.ONVIFUsername,
		password: c.ONVIFPassword,
		ptzURL: strings.TrimSpace(c.ONVIFPTZURL),
		profileToken: strings.TrimSpace(c.ONVIFProfileToken),
		http: &http.Client{Timeout: time.Duration(c.ONVIFTimeoutMS) * time.Millisecond},
	}
}

func (o *onvifClient) enabled() bool { return o != nil && o.deviceService != "" }

func xmlEsc(s string) string {
	var b bytes.Buffer
	_ = xml.EscapeText(&b, []byte(s))
	return b.String()
}

func (o *onvifClient) securityHeader() (string, error) {
	if o.username == "" { return "", nil }
	nonce := make([]byte, 16)
	if _, err := rand.Read(nonce); err != nil { return "", err }
	created := time.Now().UTC().Format("2006-01-02T15:04:05.000Z")
	h := sha1.New()
	_, _ = h.Write(nonce)
	_, _ = io.WriteString(h, created)
	_, _ = io.WriteString(h, o.password)
	digest := base64.StdEncoding.EncodeToString(h.Sum(nil))
	return fmt.Sprintf(`<s:Header><wsse:Security s:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"><wsse:UsernameToken><wsse:Username>%s</wsse:Username><wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">%s</wsse:Password><wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">%s</wsse:Nonce><wsu:Created>%s</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>`, xmlEsc(o.username), digest, base64.StdEncoding.EncodeToString(nonce), created), nil
}

func (o *onvifClient) soap(url, body string) ([]byte, error) {
	header, err := o.securityHeader()
	if err != nil { return nil, err }
	env := `<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">` + header + `<s:Body>` + body + `</s:Body></s:Envelope>`
	req, err := http.NewRequest(http.MethodPost, url, strings.NewReader(env))
	if err != nil { return nil, err }
	req.Header.Set("Content-Type", "application/soap+xml; charset=utf-8")
	resp, err := o.http.Do(req)
	if err != nil { return nil, err }
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil { return nil, err }
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("ONVIF HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	return b, nil
}

type capsEnvelope struct {
	Body struct {
		Response struct {
			Capabilities struct {
				Media struct { XAddr string `xml:"XAddr"` } `xml:"Media"`
				PTZ   struct { XAddr string `xml:"XAddr"` } `xml:"PTZ"`
			} `xml:"Capabilities"`
		} `xml:"GetCapabilitiesResponse"`
	} `xml:"Body"`
}

type profilesEnvelope struct {
	Body struct {
		Response struct {
			Profiles []struct { Token string `xml:"token,attr"` } `xml:"Profiles"`
		} `xml:"GetProfilesResponse"`
	} `xml:"Body"`
}

func (o *onvifClient) discoverLocked() error {
	if o.ptzURL != "" && o.profileToken != "" { return nil }
	body := `<tds:GetCapabilities xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tds:Category>All</tds:Category></tds:GetCapabilities>`
	b, err := o.soap(o.deviceService, body)
	if err != nil { return err }
	var caps capsEnvelope
	if err = xml.Unmarshal(b, &caps); err != nil { return err }
	if o.ptzURL == "" { o.ptzURL = strings.TrimSpace(caps.Body.Response.Capabilities.PTZ.XAddr) }
	o.mediaURL = strings.TrimSpace(caps.Body.Response.Capabilities.Media.XAddr)
	if o.ptzURL == "" { return fmt.Errorf("ONVIF camera did not return PTZ XAddr") }
	if o.profileToken == "" {
		if o.mediaURL == "" { return fmt.Errorf("ONVIF camera did not return Media XAddr") }
		b, err = o.soap(o.mediaURL, `<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>`)
		if err != nil { return err }
		var p profilesEnvelope
		if err = xml.Unmarshal(b, &p); err != nil { return err }
		if len(p.Body.Response.Profiles) == 0 || p.Body.Response.Profiles[0].Token == "" { return fmt.Errorf("ONVIF profile token not found") }
		o.profileToken = p.Body.Response.Profiles[0].Token
	}
	return nil
}

func (o *onvifClient) absoluteMove(pan, tilt, speed float64) error {
	if !o.enabled() { return nil }
	o.mu.Lock(); defer o.mu.Unlock()
	if err := o.discoverLocked(); err != nil { return err }
	if pan < -1 { pan = -1 }; if pan > 1 { pan = 1 }
	if tilt < -1 { tilt = -1 }; if tilt > 1 { tilt = 1 }
	if speed < 0.05 { speed = 0.05 }; if speed > 1 { speed = 1 }
	body := fmt.Sprintf(`<tptz:AbsoluteMove xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><tptz:ProfileToken>%s</tptz:ProfileToken><tptz:Position><tt:PanTilt x="%.5f" y="%.5f" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"/></tptz:Position><tptz:Speed><tt:PanTilt x="%.5f" y="%.5f" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace"/></tptz:Speed></tptz:AbsoluteMove>`, xmlEsc(o.profileToken), pan, tilt, speed, speed)
	_, err := o.soap(o.ptzURL, body)
	return err
}

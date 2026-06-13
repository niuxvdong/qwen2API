package upstream

import (
	"bufio"
	"encoding/json"
	"io"
	"strings"
)

// Event is a normalized chunk emitted by Qwen's SSE stream.
type Event struct {
	Type          string
	Phase         string
	Content       string
	ReasoningText string
	Status        string
	Extra         map[string]any
	Raw           map[string]any
}

// ConsumeSSE parses server-sent events from r and invokes onEvent for every
// normalized upstream message.
func ConsumeSSE(r io.Reader, onEvent func(Event) error) error {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	var block strings.Builder
	for scanner.Scan() {
		line := scanner.Text()
		if strings.TrimSpace(line) == "" {
			if err := ParseSSEBlock(block.String(), onEvent); err != nil {
				return err
			}
			block.Reset()
			continue
		}
		block.WriteString(line)
		block.WriteByte('\n')
	}
	if strings.TrimSpace(block.String()) != "" {
		if err := ParseSSEBlock(block.String(), onEvent); err != nil {
			return err
		}
	}
	return scanner.Err()
}

// ParseSSEBlock decodes one SSE block with data: JSON payloads.
func ParseSSEBlock(block string, onEvent func(Event) error) error {
	for _, line := range strings.Split(block, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "" || data == "[DONE]" {
			continue
		}
		var obj map[string]any
		if err := json.Unmarshal([]byte(data), &obj); err != nil {
			continue
		}
		for _, event := range ParseQwenEvent(obj) {
			if err := onEvent(event); err != nil {
				return err
			}
		}
	}
	return nil
}

// ParseQwenEvent normalizes the different text fields Qwen uses across models.
func ParseQwenEvent(obj map[string]any) []Event {
	events := []Event{}
	if choices, ok := obj["choices"].([]any); ok && len(choices) > 0 {
		if choice, ok := choices[0].(map[string]any); ok {
			delta, _ := choice["delta"].(map[string]any)
			phase := firstString(delta["phase"])
			if phase == "" {
				phase = "answer"
			}
			content := firstString(delta["content"])
			extra, _ := delta["extra"].(map[string]any)
			reasoning := extractReasoning(delta, extra)
			if reasoning != "" {
				content = reasoning
				if phase == "answer" {
					phase = "thinking_summary"
				}
			}
			events = append(events, Event{
				Type:          "delta",
				Phase:         phase,
				Content:       content,
				ReasoningText: reasoning,
				Status:        firstString(delta["status"]),
				Extra:         extra,
				Raw:           obj,
			})
			return events
		}
	}
	content := firstString(obj["content"], obj["answer"], obj["text"], obj["delta"])
	reasoning := firstString(obj["reasoning_content"], obj["reasoning"], obj["thinking"])
	status := firstString(obj["status"])
	eventType := firstString(obj["event"], obj["type"], status)
	if content != "" || reasoning != "" || eventType != "" {
		phase := eventType
		if phase == "" {
			phase = "answer"
		}
		if reasoning != "" {
			content = reasoning
			if phase == "answer" {
				phase = "thinking_summary"
			}
		}
		events = append(events, Event{Type: firstNonEmpty(eventType, "delta"), Phase: phase, Content: content, ReasoningText: reasoning, Status: status, Raw: obj})
	}
	if data, ok := obj["data"].(map[string]any); ok {
		events = append(events, ParseQwenEvent(data)...)
	}
	if msg, ok := obj["message"].(map[string]any); ok {
		events = append(events, ParseQwenEvent(msg)...)
	}
	return events
}

func extractReasoning(delta map[string]any, extra map[string]any) string {
	if delta == nil {
		return ""
	}
	values := []any{
		delta["reasoning_content"],
		delta["reasoning"],
		delta["reasoning_text"],
		delta["thinking"],
		delta["thoughts"],
	}
	if extra != nil {
		values = append(values, extra["reasoning_content"], extra["reasoning"], extra["reasoning_text"], extra["thinking"], extra["thoughts"])
	}
	return firstString(values...)
}

func firstString(values ...any) string {
	for _, value := range values {
		if s, ok := value.(string); ok && strings.TrimSpace(s) != "" {
			return s
		}
	}
	return ""
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

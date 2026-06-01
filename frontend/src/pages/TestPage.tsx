import { useEffect, useRef, useState, type ReactNode } from "react"
import { Button } from "../components/ui/button"
import { Send, RefreshCw, Bot } from "lucide-react"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import { toast } from "sonner"

// 渲染消息内容：自动把 Markdown 图片和图片 URL 渲染成 <img>
function MessageContent({ content }: { content: string }) {
  type Seg = { start: number; end: number; url: string }
  const segs: Seg[] = []
  const fullRe = /!\[[^\]]*\]\((https?:\/\/[^)\s]+)\)|(https?:\/\/[^\s"<>]+\.(?:jpg|jpeg|png|webp|gif)[^\s"<>]*)/gi
  let m: RegExpExecArray | null
  while ((m = fullRe.exec(content)) !== null) {
    segs.push({ start: m.index, end: m.index + m[0].length, url: (m[1] || m[2]) as string })
  }

  if (segs.length === 0) {
    return <div className="whitespace-pre-wrap leading-relaxed">{content}</div>
  }

  const nodes: ReactNode[] = []
  let cursor = 0
  segs.forEach((seg, i) => {
    if (seg.start > cursor) {
      nodes.push(<span key={"t" + i}>{content.slice(cursor, seg.start)}</span>)
    }
    nodes.push(
      <div key={"i" + i} className="my-2">
        <img
          src={seg.url}
          alt="generated"
          className="max-w-full rounded-lg shadow-md border"
          loading="lazy"
          onError={e => { (e.currentTarget as HTMLImageElement).style.display = "none" }}
        />
        <div className="text-xs text-muted-foreground mt-1 break-all font-mono">{seg.url}</div>
      </div>
    )
    cursor = seg.end
  })
  if (cursor < content.length) {
    nodes.push(<span key="tail">{content.slice(cursor)}</span>)
  }
  return <div className="whitespace-pre-wrap leading-relaxed">{nodes}</div>
}

type ChatMessage = { role: string; content: string; reasoning?: string; error?: boolean }
const TYPEWRITER_CHUNK_SIZE = 2
const TYPEWRITER_DELAY_MS = 24

function asText(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {}
}

function extractTextFromContent(content: unknown): string {
  if (typeof content === "string") return content
  if (!Array.isArray(content)) return ""
  return content
    .map(part => {
      const block = asRecord(part)
      const type = asText(block.type)
      if (type === "thinking" || type === "reasoning" || type === "reasoning_text") {
        return ""
      }
      if (type === "text" || type === "output_text" || type === "message") {
        return asText(block.text) || asText(block.content)
      }
      return asText(block.text) || asText(block.content)
    })
    .join("")
}

function readReasoningFields(value: unknown): string {
  const record = asRecord(value)
  const extra = asRecord(record.extra)
  return (
    asText(record.reasoning_content) ||
    asText(record.reasoning) ||
    asText(record.reasoning_text) ||
    asText(record.thinking) ||
    asText(record.thoughts) ||
    asText(extra.reasoning_content) ||
    asText(extra.reasoning) ||
    asText(extra.reasoning_text) ||
    asText(extra.thinking) ||
    asText(extra.thoughts)
  )
}

function splitInlineThinking(content: string, reasoning = ""): { content: string; reasoning: string } {
  if (!content || !/<think[\s>]/i.test(content)) return { content, reasoning }
  let visible = ""
  let thoughts = reasoning
  let cursor = 0
  for (const match of content.matchAll(/<think[^>]*>([\s\S]*?)<\/think>/gi)) {
    visible += content.slice(cursor, match.index)
    thoughts += match[1] || ""
    cursor = (match.index ?? 0) + match[0].length
  }
  visible += content.slice(cursor)
  return { content: visible, reasoning: thoughts }
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, ms))
}

function extractReasoningFromContent(content: unknown): string {
  if (!Array.isArray(content)) return ""
  return content
    .map(part => {
      const block = asRecord(part)
      const type = block.type
      if (type === "thinking") return asText(block.thinking)
      if (type === "reasoning_text") return asText(block.text)
      if (type === "reasoning") return asText(block.text) || asText(block.reasoning)
      return readReasoningFields(block)
    })
    .join("")
}

function normalizeAssistantMessage(message: unknown): ChatMessage {
  const msg = asRecord(message)
  const inline = splitInlineThinking(extractTextFromContent(msg.content), readReasoningFields(msg) || extractReasoningFromContent(msg.content))
  return {
    role: asText(msg.role) || "assistant",
    content: inline.content,
    ...(inline.reasoning ? { reasoning: inline.reasoning } : {}),
  }
}

function extractStreamDelta(payload: unknown): { content: string; reasoning: string } {
  const data = asRecord(payload)
  const responseEventType = asText(data.type)
  if (responseEventType === "response.reasoning_text.delta") {
    return { content: "", reasoning: asText(data.delta) }
  }
  if (responseEventType === "response.output_text.delta") {
    return splitInlineThinking(asText(data.delta))
  }

  const choices = Array.isArray(data.choices) ? data.choices : []
  const choice = asRecord(choices[0])
  const delta = asRecord(choice.delta)
  const message = asRecord(choice.message)
  const content = extractTextFromContent(delta.content) || extractTextFromContent(message.content) || extractTextFromContent(data.content)
  const reasoning = readReasoningFields(delta) || readReasoningFields(message) || readReasoningFields(data) || extractReasoningFromContent(delta.content) || extractReasoningFromContent(message.content)
  return splitInlineThinking(content, reasoning)
}

export default function TestPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState("")
  const [loading, setLoading] = useState(false)
  const [model, setModel] = useState("qwen3.6-plus")
  const [availableModels, setAvailableModels] = useState<string[]>(["qwen3.6-plus"])
  const [stream, setStream] = useState(true)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages])

  // 挂载时从 /v1/models 拉真实模型列表，失败回退到默认三项
  useEffect(() => {
    (async () => {
      try {
        const r = await fetch(`${API_BASE}/v1/models`, { headers: getAuthHeader() })
        if (!r.ok) return
        const j = await r.json()
        const ids = (j?.data || [])
          .map((m: { id?: string }) => m?.id)
          .filter((id: unknown): id is string => typeof id === "string" && !!id)
        if (ids.length) {
          setAvailableModels(ids)
          if (!ids.includes(model)) setModel(ids[0])
        }
      } catch {
        // keep fallback list
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const appendAssistantDelta = (content: string, reasoning: string) => {
    if (!content && !reasoning) return
    setMessages(prev => {
      const msgs = [...prev]
      const last = msgs[msgs.length - 1] ?? { role: "assistant", content: "" }
      msgs[msgs.length - 1] = {
        ...last,
        content: (last.content || "") + content,
        reasoning: (last.reasoning || "") + reasoning,
      }
      return msgs
    })
  }

  const appendAssistantTypewriter = async (message: ChatMessage) => {
    setMessages(prev => [...prev, { role: "assistant", content: "" }])
    let pendingReasoning = message.reasoning || ""
    let pendingContent = message.content || ""
    while (pendingReasoning || pendingContent) {
      if (pendingReasoning) {
        const chunk = pendingReasoning.slice(0, TYPEWRITER_CHUNK_SIZE)
        pendingReasoning = pendingReasoning.slice(chunk.length)
        appendAssistantDelta("", chunk)
      } else {
        const chunk = pendingContent.slice(0, TYPEWRITER_CHUNK_SIZE)
        pendingContent = pendingContent.slice(chunk.length)
        appendAssistantDelta(chunk, "")
      }
      await sleep(TYPEWRITER_DELAY_MS)
    }
  }

  const handleSend = async () => {
    if (!input.trim() || loading) return
    const userMsg = { role: "user", content: input }
    setMessages(prev => [...prev, userMsg])
    setInput("")
    setLoading(true)

    try {
      if (!stream) {
        const res = await fetch(`${API_BASE}/v1/chat/completions`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...getAuthHeader() },
          body: JSON.stringify({ model, messages: [...messages, userMsg], stream: false, include_reasoning: true })
        })
        const data = await res.json()
        if (data.error) {
          setMessages(prev => [...prev, { role: "assistant", content: `❌ ${data.error}`, error: true }])
        } else if (data.choices?.[0]) {
          await appendAssistantTypewriter(normalizeAssistantMessage(data.choices[0].message))
        } else {
          setMessages(prev => [...prev, { role: "assistant", content: `❌ 未知响应: ${JSON.stringify(data)}`, error: true }])
        }
      } else {
        const res = await fetch(`${API_BASE}/v1/chat/completions`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...getAuthHeader() },
          body: JSON.stringify({ model, messages: [...messages, userMsg], stream: true, include_reasoning: true })
        })

        if (!res.ok) {
          const errText = await res.text()
          setMessages(prev => [...prev, { role: "assistant", content: `❌ HTTP ${res.status}: ${errText}`, error: true }])
          return
        }

        if (!res.body) throw new Error("No response body")

        setMessages(prev => [...prev, { role: "assistant", content: "" }])
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let hasContent = false
        let hasTerminalError = false
        const outputQueue = { content: "", reasoning: "" }
        let typewriterRunning = false

        const runTypewriter = async () => {
          if (typewriterRunning) return
          typewriterRunning = true
          try {
            while (outputQueue.reasoning || outputQueue.content) {
              if (outputQueue.reasoning) {
                const chunk = outputQueue.reasoning.slice(0, TYPEWRITER_CHUNK_SIZE)
                outputQueue.reasoning = outputQueue.reasoning.slice(chunk.length)
                appendAssistantDelta("", chunk)
              } else {
                const chunk = outputQueue.content.slice(0, TYPEWRITER_CHUNK_SIZE)
                outputQueue.content = outputQueue.content.slice(chunk.length)
                appendAssistantDelta(chunk, "")
              }
              await sleep(TYPEWRITER_DELAY_MS)
            }
          } finally {
            typewriterRunning = false
            if (outputQueue.reasoning || outputQueue.content) void runTypewriter()
          }
        }

        const enqueueAssistantDelta = (content: string, reasoning: string) => {
          if (!content && !reasoning) return
          hasContent = true
          outputQueue.content += content
          outputQueue.reasoning += reasoning
          void runTypewriter()
        }

        const waitForTypewriter = async () => {
          while (typewriterRunning || outputQueue.reasoning || outputQueue.content) {
            await sleep(20)
          }
        }

        let currentEventData = ""

        const processSsePayload = (payload: string) => {
          const trimmedPayload = payload.trim()
          if (!trimmedPayload || trimmedPayload === "[DONE]") return

          try {
            const data = JSON.parse(trimmedPayload)
            if (data.error) {
              outputQueue.content = ""
              outputQueue.reasoning = ""
              setMessages(prev => {
                const msgs = [...prev]
                msgs[msgs.length - 1] = { role: "assistant", content: `❌ ${data.error}`, error: true }
                return msgs
              })
              hasContent = true
              hasTerminalError = true
              return
            }
            const { content, reasoning } = extractStreamDelta(data)
            enqueueAssistantDelta(content, reasoning)
          } catch {
            // Keep the test page resilient to malformed payloads without aborting the stream.
          }
        }

        let buffer = ""

        const dispatchSseEvent = () => {
          if (!currentEventData) return
          const payload = currentEventData
          currentEventData = ""
          processSsePayload(payload)
        }

        const processSseLine = (rawLine: string) => {
          const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine
          if (line === "") {
            dispatchSseEvent()
            return
          }
          if (line.startsWith(":")) return
          if (!line.startsWith("data:")) return

          const data = line.startsWith("data: ") ? line.slice(6) : line.slice(5)
          currentEventData += currentEventData ? `\n${data}` : data
        }

        const processSseChunk = (chunk: string) => {
          if (!chunk) return
          buffer += chunk
          const lines = buffer.split("\n")
          buffer = lines.pop() ?? ""
          for (const line of lines) {
            processSseLine(line)
            if (hasTerminalError) break
          }
        }

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          processSseChunk(decoder.decode(value, { stream: true }))
          if (hasTerminalError) break
        }

        if (!hasTerminalError) {
          processSseChunk(decoder.decode())
          if (buffer) {
            processSseLine(buffer)
            buffer = ""
          }
          dispatchSseEvent()
        } else {
          decoder.decode()
        }

        await waitForTypewriter()

        if (!hasContent) {
          setMessages(prev => {
            const msgs = [...prev]
            msgs[msgs.length - 1] = { role: "assistant", content: "❌ 响应为空（账号可能未激活或无可用账号）", error: true }
            return msgs
          })
        }
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "未知错误"
      toast.error(`网络错误: ${message}`)
      setMessages(prev => [...prev, { role: "assistant", content: `❌ 网络错误: ${message}`, error: true }])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col h-[calc(100vh-10rem)] space-y-4 max-w-5xl mx-auto">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">接口测试</h2>
          <p className="text-muted-foreground">在此测试您的 API 分发是否正常工作。</p>
        </div>
        <div className="flex gap-4 items-center">
          <div className="flex items-center gap-2 text-sm bg-card border px-3 py-1.5 rounded-md">
            <span className="font-medium text-muted-foreground">模型:</span>
            <select value={model} onChange={e => setModel(e.target.value)} className="bg-transparent font-mono outline-none">
              {availableModels.map(id => (
                <option key={id} value={id}>{id}</option>
              ))}
            </select>
          </div>
          <div
            className="flex items-center gap-2 text-sm bg-card border px-3 py-1.5 rounded-md cursor-pointer"
            onClick={() => setStream(!stream)}
          >
            <input type="checkbox" checked={stream} onChange={() => {}} className="cursor-pointer" />
            <span className="font-medium">流式传输 (Stream)</span>
          </div>
          <Button variant="outline" onClick={() => setMessages([])}>
            <RefreshCw className="mr-2 h-4 w-4" /> 清空对话
          </Button>
        </div>
      </div>

      <div className="flex-1 rounded-xl border bg-card overflow-hidden flex flex-col shadow-sm">
        <div className="flex-1 overflow-y-auto p-6 space-y-6 flex flex-col">
          {messages.length === 0 && (
            <div className="h-full flex flex-col items-center justify-center text-muted-foreground space-y-4">
              <Bot className="h-12 w-12 text-muted-foreground/30" />
              <p className="text-sm">发送一条消息以开始测试，系统将通过 /v1/chat/completions 进行调用。</p>
            </div>
          )}
          {messages.map((msg, i) => (
            <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
              <div className={`max-w-[80%] rounded-xl px-4 py-3 text-sm shadow-sm
                ${msg.role === "user"
                  ? "bg-primary text-primary-foreground"
                  : msg.error
                    ? "bg-red-500/10 border border-red-500/30 text-red-400"
                    : "bg-muted/30 border text-foreground"}`}>
                {msg.role === "assistant" && !msg.content && !msg.reasoning && loading ? (
                  <span className="animate-pulse flex items-center gap-2 text-muted-foreground">
                    <Bot className="h-4 w-4" /> 思考中...
                  </span>
                ) : msg.role === "assistant" && !msg.error ? (
                  <div className="space-y-2">
                    {msg.reasoning ? (
                      <details open className="rounded-md border border-dashed border-border/50 bg-muted/20 p-2 text-xs">
                        <summary className="cursor-pointer select-none text-muted-foreground font-mono">
                          💭 思考过程 ({msg.reasoning.length} 字)
                        </summary>
                        <div className="whitespace-pre-wrap leading-relaxed text-muted-foreground mt-2 pl-2 border-l-2 border-border/30">
                          {msg.reasoning}
                        </div>
                      </details>
                    ) : null}
                    {msg.content ? <MessageContent content={msg.content} /> : null}
                  </div>
                ) : (
                  <div className="whitespace-pre-wrap leading-relaxed">{msg.content}</div>
                )}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        <div className="p-4 border-t bg-muted/30 flex gap-3 items-center">
          <input
            type="text"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === "Enter" && handleSend()}
            className="flex h-12 w-full rounded-md border border-input bg-background px-4 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
            placeholder="输入测试消息..."
            disabled={loading}
          />
          <Button onClick={handleSend} disabled={loading || !input.trim()} className="h-12 px-6">
            {loading ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          </Button>
        </div>
      </div>
    </div>
  )
}

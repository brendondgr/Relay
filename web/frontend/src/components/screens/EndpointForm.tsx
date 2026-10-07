import React from 'react';
import type { CSSProperties } from 'react';
import type { EndpointOut, ModelFactsOverride } from '../../lib/types';
import { C, MONO, SANS } from '../../lib/styles';

/** Shared add/edit endpoint form.
 *
 * Extracted from Endpoints.tsx, which was at the ~850-line ceiling: the two
 * forms had drifted into near-duplicates, and every new field had to be added
 * twice. One component, two call sites.
 */

export const inputStyle: CSSProperties = {
  background: C.bgSidebar,
  border: `1px solid ${C.borderStrong}`,
  borderRadius: 6,
  color: C.text,
  padding: '7px 10px',
  font: `400 12px ${MONO}`,
  outline: 'none',
  // border-box + full width so inputs stay inside their grid columns.
  boxSizing: 'border-box',
  width: '100%',
  minWidth: 0,
};

export const selectStyle: CSSProperties = {
  ...inputStyle,
  padding: '7px 8px',
};

/** Server types that are agent protocols rather than OpenAI-compatible ones.
 * The `protocol` sent to the API is derived from this, never picked directly —
 * two fields that must agree are one field the UI can get wrong. */
const AGENT_TYPES = new Set(['opencode']);

export const protocolFor = (serverType: string): 'openai' | 'opencode' =>
  AGENT_TYPES.has(serverType) ? 'opencode' : 'openai';

export interface EndpointFormValues {
  name: string;
  type: string;
  alias: string;
  url: string;
  key: string;
  /** One model id per line (commas accepted too) — see parseModels. */
  models: string;
  /** Model-facts override: '' = detect from the upstream. */
  ctx: string;
  tools: Tri;
  vision: Tri;
  reasoning: Tri;
}

/** A fact the operator can force: '' leaves it to detection. */
type Tri = '' | 'yes' | 'no';
const FACT_FLAGS = ['tools', 'vision', 'reasoning'] as const;

const triFrom = (v: boolean | null | undefined): Tri => (v == null ? '' : v ? 'yes' : 'no');

export const emptyForm = (): EndpointFormValues => ({
  name: '', type: 'llama.cpp', alias: '', url: '', key: '', models: '',
  ctx: '', tools: '', vision: '', reasoning: '',
});

export const formFromEndpoint = (e: EndpointOut): EndpointFormValues => ({
  name: e.name,
  type: e.server_type,
  alias: e.alias ?? '',
  url: e.base_url,
  key: '',
  models: (e.available_models ?? []).join('\n'),
  ctx: e.facts_override?.context_length ? String(e.facts_override.context_length) : '',
  tools: triFrom(e.facts_override?.tools),
  vision: triFrom(e.facts_override?.vision),
  reasoning: triFrom(e.facts_override?.reasoning),
});

/** Form -> the facts_override the API expects. Always an object, so an edit
 * that blanks every field clears the stored override ({} clears). */
export const factsFromForm = (v: EndpointFormValues): Partial<ModelFactsOverride> => {
  const out: Partial<ModelFactsOverride> = {};
  const ctx = parseInt(v.ctx.replace(/[_,\s]/g, ''), 10);
  if (ctx > 0) out.context_length = ctx;
  for (const f of FACT_FLAGS) if (v[f]) out[f] = v[f] === 'yes';
  return out;
};

/** Textarea contents -> the model allowlist the API expects. */
export const parseModels = (text: string): string[] =>
  text.split(/[\n,]/).map(s => s.trim()).filter(Boolean);

interface Props {
  values: EndpointFormValues;
  onChange: (patch: Partial<EndpointFormValues>) => void;
  onSave: () => void;
  onCancel: () => void;
  saveLabel: string;
  /** Edit mode leaves the stored key untouched when this is left blank. */
  keepKeyHint?: boolean;
  /** Extra rows (e.g. the ssh tunnel picker) rendered above the footer. */
  children?: React.ReactNode;
}

const buttonBase: CSSProperties = {
  padding: '6px 14px',
  borderRadius: 6,
  font: `600 12px ${SANS}`,
  cursor: 'pointer',
  userSelect: 'none',
};

export default function EndpointForm(props: Props): React.JSX.Element {
  const { values, onChange, onSave, onCancel, saveLabel, keepKeyHint, children } = props;
  const isAgent = protocolFor(values.type) === 'opencode';
  const keyPlaceholder =
    keepKeyHint ? 'api key (leave blank to keep)'
    : isAgent ? 'user:password (Basic auth)'
    : 'api key (optional)';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 130px 110px 1fr 1fr', gap: 10 }}>
        <input
          placeholder="name"
          value={values.name}
          onChange={(e) => onChange({ name: e.target.value })}
          style={inputStyle}
        />
        <select
          value={values.type}
          onChange={(e) => onChange({ type: e.target.value })}
          style={selectStyle}
        >
          <option value="llama.cpp">llama.cpp</option>
          <option value="vLLM">vLLM</option>
          <option value="ollama">ollama</option>
          <option value="openai">OpenAI-compat</option>
          <option value="opencode">OpenCode (agent)</option>
        </select>
        <input
          placeholder={isAgent ? 'alias (agent)' : 'alias (skynet)'}
          value={values.alias}
          onChange={(e) => onChange({ alias: e.target.value })}
          style={inputStyle}
        />
        <input
          placeholder={isAgent ? 'http://127.0.0.1:4096' : 'http://127.0.0.1:8000/v1'}
          value={values.url}
          onChange={(e) => onChange({ url: e.target.value })}
          style={inputStyle}
        />
        <input
          placeholder={keyPlaceholder}
          value={values.key}
          onChange={(e) => onChange({ key: e.target.value })}
          style={inputStyle}
        />
      </div>

      <textarea
        placeholder={isAgent
          ? 'available models — one per line, e.g. anthropic/claude-sonnet-4-5'
          : 'available models (optional) — one per line'}
        value={values.models}
        onChange={(e) => onChange({ models: e.target.value })}
        rows={isAgent ? 4 : 2}
        style={{ ...inputStyle, resize: 'vertical', lineHeight: 1.5 }}
      />
      <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim }}>
        Every id listed here is <b style={{ color: C.textMut }}>routable and advertised</b>: it shows up in
        <span style={{ fontFamily: MONO }}> GET /v1/models</span> and a client that sends it as the
        <span style={{ fontFamily: MONO }}> model</span> reaches this endpoint with that exact model.
        {isAgent
          ? <> OpenCode ids are <span style={{ fontFamily: MONO }}>provider/model</span>, e.g.{' '}
              <span style={{ fontFamily: MONO }}>anthropic/claude-sonnet-4-5</span>. Leave empty to serve only the
              alias, on whatever model the server defaults to.</>
          : ' Leave empty to advertise just the alias.'}
      </span>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 120px 120px 120px', gap: 10 }}>
        <input
          placeholder="context window (detected)"
          value={values.ctx}
          onChange={(e) => onChange({ ctx: e.target.value })}
          style={inputStyle}
        />
        {FACT_FLAGS.map(f => (
          <select
            key={f}
            value={values[f]}
            onChange={(e) => onChange({ [f]: e.target.value as Tri } as Partial<EndpointFormValues>)}
            style={selectStyle}
          >
            <option value="">{f}: detect</option>
            <option value="yes">{f}: yes</option>
            <option value="no">{f}: no</option>
          </select>
        ))}
      </div>
      <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim }}>
        <b style={{ color: C.textMut }}>Model facts</b> shown as <span style={{ fontFamily: MONO }}>meta</span> in
        <span style={{ fontFamily: MONO }}> GET /v1/models</span>. relay reads them from the upstream (llama.cpp
        <span style={{ fontFamily: MONO }}> /props</span>, vLLM <span style={{ fontFamily: MONO }}>max_model_len</span>,
        or a probe through another relay); anything set here wins.
      </span>

      {isAgent && (
        <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim }}>
          <b style={{ color: C.textMut }}>OpenCode</b> is an agent server, not a model server: relay translates each
          request into an ephemeral session against a running{' '}
          <span style={{ fontFamily: MONO }}>opencode serve</span>. It is reachable
          <b style={{ color: C.textMut }}> only by name</b> — never through{' '}
          <span style={{ fontFamily: MONO }}>"model": "auto"</span> or failover. It also runs shell commands on its
          host, so put it behind a client key.
        </span>
      )}

      {children}

      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <div
          onClick={onCancel}
          style={{ ...buttonBase, border: `1px solid ${C.borderStrong}`, color: C.textMut, fontWeight: 500 }}
        >
          Cancel
        </div>
        <div onClick={onSave} style={{ ...buttonBase, background: '#3FB950', color: '#0B0E14' }}>
          {saveLabel}
        </div>
      </div>
    </div>
  );
}

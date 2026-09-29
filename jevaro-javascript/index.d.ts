import type { AsyncRecordBatchStreamReader } from "apache-arrow";
import type { Questions, SystemOneRequest as OriginalRequest } from "@typesafe-ai/sdk";

export { choice, noul, score } from "@typesafe-ai/sdk";
export type { ChoiceQuestion, NoulQuestion, ScoreQuestion, Question, Questions } from "@typesafe-ai/sdk";

export type State = NonNullable<OriginalRequest["state"]>;
export type SystemOneRequest<Q extends Questions = Questions> = {
  questions: Q;
  model?: string;
} & ({ state: State; states?: never } | { states: State[]; state?: never });

export interface TypeSafeClientConfig {
  apiKey?: string;
  baseURL?: string;
  defaultModel?: string;
  defaultHeaders?: HeadersInit;
  /** Milliseconds to receive the schema, not a time limit on the whole batch. */
  timeout?: number;
  fetch?: typeof globalThis.fetch;
}

export interface RequestOptions {
  headers?: HeadersInit;
  timeout?: number;
  signal?: AbortSignal;
}

export class APIError extends Error {
  readonly status: number;
  readonly body: unknown;
}

export class TypeSafeClient {
  constructor(config?: TypeSafeClientConfig);
  systemOne<Q extends Questions>(
    request: SystemOneRequest<Q>, options?: RequestOptions,
  ): Promise<AsyncRecordBatchStreamReader>;
}

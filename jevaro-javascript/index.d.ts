import type { AsyncRecordBatchStreamReader, Table } from "apache-arrow";
import type { Image, Questions, SystemOneRequest as OriginalRequest } from "@typesafe-ai/sdk";

export { box, choice, Image, image, noul, score, withAssets } from "@typesafe-ai/sdk";
export type {
  BoxQuestion, ChoiceQuestion, NoulQuestion, ScoreQuestion, Question, Questions, TextWithAssets,
} from "@typesafe-ai/sdk";

/** Text, an image, or a JSON object or array, which may hold images. */
export type State = NonNullable<OriginalRequest["state"]>;
export type SystemOneRequest<Q extends Questions = Questions> = {
  questions: Q;
  model?: string;
  /** States per upstream call, 1 to 256; above 1, each state moves into its questions' instructions. */
  rowsPerCall?: number;
  /** Images under names you choose; write each name in a state, instructions, or criteria. */
  assets?: Record<string, Image>;
  /** `"geoarrow"` writes each Box answer's edges as a `geoarrow.box`; the default keeps TypeSafe's names. */
  boxFormat?: "typesafe" | "geoarrow";
} & (
  | { state: State; states?: never; stateColumn?: never }
  | { states: State[]; state?: never; stateColumn?: never }
  /** Each row is one state: an object of its columns, or the value in `stateColumn`. Binary values are images. */
  | { states: Table; stateColumn?: string; state?: never }
);

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

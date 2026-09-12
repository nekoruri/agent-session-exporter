import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";

// Exercise the actual migration/SQL and batch rollback, with D1's asynchronous interface.
export class TestD1 {
  constructor() {
    this.bindings = [];
    this.connection = new DatabaseSync(":memory:");
    for (const file of ["0001_events.sql", "0002_message_buffer.sql", "0003_event_identity.sql"])
      this.connection.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  }
  get rows() { return this.connection.prepare("SELECT * FROM events ORDER BY id").all(); }
  prepare(sql) {
    const statement = this.connection.prepare(sql);
    const wrapper = (values) => ({
      bind: (...args) => { this.bindings.push(args); return wrapper(args); },
      run: () => ({ meta: { changes: Number(statement.run(...values).changes) } }),
      all: async () => ({ results: statement.all(...values) }),
      first: async () => statement.get(...values) ?? null,
    });
    return wrapper([]);
  }
  async batch(statements) {
    this.connection.exec("BEGIN");
    try {
      const result = [];
      for (const statement of statements) result.push(statement.run());
      this.connection.exec("COMMIT");
      return result;
    } catch (error) {
      this.connection.exec("ROLLBACK");
      throw error;
    }
  }
}

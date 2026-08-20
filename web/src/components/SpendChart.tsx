/**
 * Line chart for anything over time — §2.6.
 *
 * Thin lines, small dot markers at data points, muted gridlines, no chart
 * border, transparent background. Recharts is already a dependency.
 */
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { money } from '../lib/format';

export interface SeriesPoint {
  label: string;
  value: number;
  secondary?: number;
}

export function SpendChart({
  data,
  valueLabel,
  secondaryLabel,
  format = money,
}: {
  data: SeriesPoint[];
  valueLabel: string;
  secondaryLabel?: string;
  format?: (value: number) => string;
}) {
  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
          <CartesianGrid stroke="var(--border-subtle)" vertical={false} />
          <XAxis
            dataKey="label"
            tick={{ fill: 'var(--text-secondary)', fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: 'var(--border-subtle)' }}
            minTickGap={24}
          />
          <YAxis
            tick={{ fill: 'var(--text-secondary)', fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={64}
            tickFormatter={(value: number) => format(value)}
          />
          <Tooltip
            contentStyle={{
              background: 'var(--bg-surface)',
              border: '1px solid var(--border-subtle)',
              borderRadius: 6,
              fontSize: 12,
            }}
            labelStyle={{ color: 'var(--text-secondary)' }}
            formatter={(value: number, name: string) => [format(value), name]}
          />
          <Line
            type="monotone"
            dataKey="value"
            name={valueLabel}
            stroke="var(--accent-info)"
            strokeWidth={1.5}
            dot={{ r: 2, fill: 'var(--accent-info)', strokeWidth: 0 }}
            activeDot={{ r: 3 }}
          />
          {secondaryLabel && (
            <Line
              type="monotone"
              dataKey="secondary"
              name={secondaryLabel}
              stroke="var(--accent-positive)"
              strokeWidth={1.5}
              dot={{ r: 2, fill: 'var(--accent-positive)', strokeWidth: 0 }}
              activeDot={{ r: 3 }}
            />
          )}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

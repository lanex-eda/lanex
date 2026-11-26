`timescale 1ns / 1ps
// Testbench for traffic. A model of the light, written from the spec rather
// than copied from the design, runs beside it; the lights are compared every
// cycle. The bench also checks that exactly one light is on, that a walk
// request cuts green short, and that the light keeps cycling. It ends with
// PASS, or with FAIL and a non-zero exit. A watchdog stops a bench that hangs.
module traffic_tb;
    // Times print in ns: %t follows $timeformat (without it, a time prints
    // in the simulator's picosecond steps).
    initial $timeformat(-9, 3, " ns", 0);
    // Short times so the simulation is quick; the chip uses the defaults.
    localparam G = 8, Y = 3, R = 5, MING = 3;

    reg  clk = 1'b0;
    reg  rst = 1'b1;
    reg  walk = 1'b0;
    wire green, yellow, red;

    traffic #(.GREEN_T(G), .YELLOW_T(Y), .RED_T(R), .MIN_GREEN(MING))
        dut (.clk(clk), .rst(rst), .walk(walk), .green(green), .yellow(yellow), .red(red));

    always #5 clk = ~clk;

    // The model: which light, and for how many more cycles.
    integer m_light = 2;          // 0 green, 1 yellow, 2 red
    integer m_left  = R;          // cycles left in this light
    integer m_time  = 0;          // cycles spent in this light
    reg     m_wait  = 1'b0;   // a walk request seen in an earlier cycle
    integer was;
    always @(posedge clk) begin
        if (rst) begin
            m_light = 2; m_left = R; m_time = 0; m_wait = 1'b0;
        end else begin
            was = m_light;
            // This cycle's change uses the requests of earlier cycles.
            if (m_left == 1 || (m_light == 0 && m_wait && m_time >= MING - 1)) begin
                m_light = (m_light + 1) % 3;
                m_left  = (m_light == 0) ? G : (m_light == 1) ? Y : R;
                m_time  = 0;
            end else begin
                m_left = m_left - 1;
                m_time = m_time + 1;
            end
            // A press during green is remembered; red forgets it.
            if (walk && was == 0) m_wait = 1'b1;
            else if (was == 2) m_wait = 1'b0;
        end
    end

    integer errors = 0;
    integer cycles = 0;
    integer greens = 0;
    integer short_green = 0;
    integer green_len = 0;
    integer i;

    task check;
        begin
            if ({green, yellow, red} !== {m_light == 0, m_light == 1, m_light == 2}) begin
                errors = errors + 1;
                $display("FAIL at %t: lights g%b y%b r%b, expected the %0s light", $realtime, green, yellow, red,
                         m_light == 0 ? "green" : m_light == 1 ? "yellow" : "red");
            end
            if (green + yellow + red !== 1) begin
                errors = errors + 1;
                $display("FAIL at %t: %0d lights on at once", $realtime, green + yellow + red);
            end
        end
    endtask

    // Watchdog: the bench must finish, whatever the design does.
    initial begin
        #100_000;
        $fatal(1, "FAIL: traffic, timeout: the bench never finished");
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, traffic_tb);
        // Inputs change on the falling edge, away from the rising edge the
        // design samples on.
        repeat (2) @(negedge clk);
        rst = 1'b0;
        for (i = 0; i < 120; i = i + 1) begin
            // Press walk once, a few cycles into the third green.
            walk = (greens == 3 && green_len == 1);
            @(negedge clk);
            check;
            cycles = cycles + 1;
            if (green) green_len = green_len + 1;
            else if (green_len > 0) begin
                if (green_len < G) short_green = short_green + 1;
                greens = greens + 1;
                green_len = 0;
            end
        end
        if (greens < 3) begin
            errors = errors + 1;
            $display("FAIL: green came %0d times in %0d cycles, expected at least 3", greens, cycles);
        end
        if (short_green != 1) begin
            errors = errors + 1;
            $display("FAIL: the walk request cut %0d greens short, expected 1", short_green);
        end
        if (errors == 0) $display("PASS: traffic, %0d cycles against the model, %0d greens, the walk request cut one short", cycles, greens);
        else $fatal(1, "FAIL: traffic, %0d errors", errors);
        $finish;
    end
endmodule

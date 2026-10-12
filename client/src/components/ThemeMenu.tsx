import { SunMoonIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { loadTheme, setTheme, THEME_CHOICES, THEME_LABELS, type ThemeChoice } from "../theme.ts";

/** 顶部栏里的「主题」菜单：跟随系统、浅色、深色。选了马上生效，并记在这个浏览器里。 */
export function ThemeMenu() {
  const [choice, setChoice] = useState<ThemeChoice>(loadTheme);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" />}>
        <SunMoonIcon data-icon="inline-start" />
        主题
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          <DropdownMenuLabel>外观</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={choice}
            onValueChange={(value: ThemeChoice) => {
              setChoice(value);
              setTheme(value);
            }}
          >
            {THEME_CHOICES.map((value) => (
              <DropdownMenuRadioItem key={value} value={value} closeOnClick>
                {THEME_LABELS[value]}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

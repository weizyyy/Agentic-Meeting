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
import { useI18n } from "../i18n/index.ts";
import { loadTheme, setTheme, THEME_CHOICES, type ThemeChoice } from "../theme.ts";

/** 顶部栏里的「主题」菜单：跟随系统、浅色、深色。选了马上生效，并记在这个浏览器里。 */
export function ThemeMenu() {
  const { t } = useI18n();
  const [choice, setChoice] = useState<ThemeChoice>(loadTheme);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" />}>
        <SunMoonIcon data-icon="inline-start" />
        {t.header.theme}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{t.header.appearance}</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={choice}
            onValueChange={(value: ThemeChoice) => {
              setChoice(value);
              setTheme(value);
            }}
          >
            {THEME_CHOICES.map((value) => (
              <DropdownMenuRadioItem key={value} value={value} closeOnClick>
                {t.header.themes[value]}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
